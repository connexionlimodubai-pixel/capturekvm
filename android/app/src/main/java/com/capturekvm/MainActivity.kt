package com.capturekvm

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.hardware.usb.UsbDevice
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.SurfaceHolder
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.ImageView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import com.capturekvm.databinding.ActivityMainBinding
import com.capturekvm.ui.GamepadView
import com.capturekvm.ui.TouchpadView
import com.capturekvm.wire.BeamerLink
import com.capturekvm.wire.KeyNames
import com.capturekvm.wire.PadWire
import com.capturekvm.wire.Pairing
import com.capturekvm.wire.Wire
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.herohan.uvcapp.CameraHelper
import com.herohan.uvcapp.ICameraHelper
import com.serenegiant.usb.Size
import kotlin.concurrent.thread
import kotlin.math.roundToInt
import org.json.JSONObject

/**
 * The one activity: a home screen, the stream, and the panels laid over them.
 *
 * Laid out after Steam Link. The home screen says what is ready -- a card, a paired PC, an input,
 * a connection -- each with its own tick, and has one big button. The stream has nothing on it
 * but the picture and two small round buttons, the menu and the keyboard, where Steam Link keeps
 * its own. Everything else lives in the menu.
 *
 * The video never goes near the network. It comes in over USB from the capture card and is drawn
 * straight to a surface, so what you see is as current as the card can make it. Only key presses,
 * pointer movement and pad reports travel, over the Beamer link to the PC.
 *
 * Three input modes, because one set of touches cannot mean three things:
 *
 *  * **Touchpad** -- the picture is a trackpad, sending mouse and keyboard.
 *  * **On-screen controller** -- a pad drawn over the picture ([com.capturekvm.ui.GamepadView]),
 *    for a phone with nothing plugged into it.
 *  * **Controller** -- a real pad read through Android's input events; the touchscreen is left
 *    alone so a stray thumb cannot move the PC's pointer mid-game.
 *
 * The last two send the identical thing: a [PadWire.State] in XUSB units, into the same virtual
 * Xbox pad on the PC. Only one is live at a time, so the two cannot fight over a button.
 */
class MainActivity : AppCompatActivity(), ICameraHelper.StateCallback, TouchpadView.Listener {

    private enum class Mode(val key: String) { TOUCH("touch"), ONSCREEN("onscreen"), PAD("pad") }

    /** A home-screen tick: ready, on its way, missing, or not tried yet. */
    private enum class Check { OK, PENDING, BAD, IDLE }

    private companion object {
        const val REQUEST_AUDIO = 1001
    }

    private lateinit var views: ActivityMainBinding
    private lateinit var store: PeerStore

    private var camera: ICameraHelper? = null
    private var surfaceReady = false
    private var cameraOpen = false
    private var attached: UsbDevice? = null

    private var link: BeamerLink? = null
    private val audio by lazy { AudioPassthrough(this) { text -> main.post { toast(text) } } }
    private var mode = Mode.TOUCH
    private var padAttached = false

    private val main = Handler(Looper.getMainLooper())
    private val pad = PadReader { state -> sendPad(state) }
    private val rumbler = Rumbler(this)

    private var cardText = ""
    /** A passing remark for the menu's header: a clipboard sent, a pairing made. */
    private var note = ""
    /** What the link last complained of, while it is trying again. Cleared once it is up. */
    private var linkError: String? = null
    /** Whether this link has been up yet, so a drop reads "reconnecting" and not "connecting". */
    private var wasUp = false

    // Which layers are up. The stream is whatever shows when the home screen is not.
    private var homeShown = true
    private var menuShown = false
    private var settingsShown = false

    /**
     * Back closes whatever panel is open, and from the stream opens the menu. It never drops the
     * link by itself -- a thumb grazing Back mid-game should not end the session -- so leaving
     * means choosing Stop streaming. From the home screen Back does what it always does.
     */
    private val backHandler = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() {
            when {
                settingsShown -> closeSettings()
                menuShown -> setMenu(false)
                !homeShown -> setMenu(true)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        views = ActivityMainBinding.inflate(layoutInflater)
        setContentView(views.root)
        store = PeerStore(this)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        goImmersive()

        views.touchpad.listener = this
        views.touchpad.scale = store.mouseScale

        // The on-screen controller reaches the PC by exactly the route a real one does.
        views.onscreenPad.listener = GamepadView.Listener { state -> sendPad(state) }
        views.onscreenPad.opacity = store.padOpacity

        views.keyboardSink.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD

        views.preview.holder.addCallback(object : SurfaceHolder.Callback {
            override fun surfaceCreated(holder: SurfaceHolder) {
                surfaceReady = true
                attachSurface()
            }

            override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) = Unit

            override fun surfaceDestroyed(holder: SurfaceHolder) {
                surfaceReady = false
                runCatching { camera?.removeSurface(holder.surface) }
            }
        })

        wireScreens()
        mode = Mode.values().firstOrNull { it.key == store.inputMode } ?: Mode.TOUCH
        applyMode()
        cardText = getString(R.string.no_card)
        showScreens()
    }

    // --- the capture card ---

    override fun onStart() {
        super.onStart()
        camera = CameraHelper().apply {
            setStateCallback(this@MainActivity)
            // A card already plugged in raises no attach event, so the list is checked once.
            val devices = runCatching { deviceList }.getOrNull().orEmpty()
            devices.firstOrNull()?.let { selectDevice(it) }
        }
    }

    override fun onStop() {
        runCatching { camera?.releaseAll() }
        camera = null
        cameraOpen = false
        super.onStop()
    }

    override fun onAttach(device: UsbDevice) {
        attached = device
        cardText = getString(R.string.waiting_signal)
        showStatus()
        // selectDevice asks for permission if it is not held; onDeviceOpen follows.
        runCatching { camera?.selectDevice(device) }
    }

    override fun onDeviceOpen(device: UsbDevice, isFirstOpen: Boolean) {
        runCatching { camera?.openCamera(bestSize()) }
    }

    override fun onCameraOpen(device: UsbDevice) {
        cameraOpen = true
        val helper = camera ?: return
        val size = runCatching { helper.previewSize }.getOrNull()
        if (size != null && size.width > 0 && size.height > 0) {
            views.preview.setAspectRatio(size.width, size.height)
            cardText = getString(R.string.card_ready) + " · ${size.width}×${size.height} @${size.fps}"
        } else {
            cardText = getString(R.string.waiting_signal)
        }
        showStatus()
        attachSurface()
        runCatching { helper.startPreview() }
    }

    override fun onCameraClose(device: UsbDevice) {
        cameraOpen = false
        runCatching { camera?.removeSurface(views.preview.holder.surface) }
    }

    override fun onDeviceClose(device: UsbDevice) = Unit

    override fun onDetach(device: UsbDevice) {
        attached = null
        cameraOpen = false
        cardText = getString(R.string.no_card)
        showStatus()
    }

    override fun onCancel(device: UsbDevice) {
        // Without the permission there is no card as far as this app is concerned.
        attached = null
        cardText = "USB permission refused"
        showStatus()
    }

    private fun attachSurface() {
        if (!surfaceReady || !cameraOpen) return
        runCatching { camera?.addSurface(views.preview.holder.surface, false) }
    }

    /**
     * The largest size the card offers at the highest rate it offers for that size. A USB 2.0
     * grabber tops out at 1080p30 in MJPEG; a USB 3.0 one will offer 1080p60, and taking the
     * fastest at the biggest size is right for both.
     */
    private fun bestSize(): Size? {
        val sizes = runCatching { camera?.supportedSizeList }.getOrNull().orEmpty()
        if (sizes.isEmpty()) return null
        return sizes.maxWithOrNull(
            compareBy({ it.width.toLong() * it.height }, { it.fps })
        )
    }

    // --- the card's sound, played here ---

    private fun toggleSound() {
        if (audio.isRunning) {
            audio.stop()
            showStatus()
            return
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            // Android has no "record this USB device only" permission, so playing the card's
            // sound needs the microphone one. Nothing is stored or sent; see the manifest.
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_AUDIO)
            return
        }
        audio.start()
        showStatus()
    }

    override fun onRequestPermissionsResult(code: Int, permissions: Array<out String>, granted: IntArray) {
        super.onRequestPermissionsResult(code, permissions, granted)
        if (code == REQUEST_AUDIO) {
            if (granted.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
                toggleSound()
            } else {
                toast("Without the microphone permission the card's sound cannot be played")
            }
        }
    }

    // --- the clipboard (upstream Beamer's capability) ---

    private fun sendClipboard() {
        val current = link ?: return
        if (!current.isUp) return
        val manager = getSystemService(ClipboardManager::class.java) ?: return
        // Android 10 and later only let the focused app read the clipboard, which this is while
        // it is driving; a null here is that rule, not an error.
        val text = manager.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        if (text.isNullOrEmpty()) return
        if (current.sendClipboard(text)) note = "Clipboard sent (${text.length} characters)"
        showStatus()
    }

    private fun receiveClipboard(text: String) {
        val manager = getSystemService(ClipboardManager::class.java) ?: return
        runCatching { manager.setPrimaryClip(ClipData.newPlainText("CaptureKVM", text)) }
            .onSuccess {
                note = "Clipboard from the PC (${text.length} characters)"
                showStatus()
            }
    }

    // --- pairing ---

    private fun askToPair() {
        val address = android.widget.EditText(this).apply {
            hint = getString(R.string.pair_address)
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val code = android.widget.EditText(this).apply {
            hint = getString(R.string.pair_code)
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        val column = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(56, 16, 56, 0)
            addView(address)
            addView(code)
            addView(TextView(this@MainActivity).apply {
                text = getString(R.string.pair_hint)
                textSize = 12f
                setTextColor(getColor(R.color.steam_text))
                setPadding(0, 24, 0, 0)
            })
        }
        MaterialAlertDialogBuilder(this, R.style.Theme_CaptureKVM_Dialog)
            .setTitle(R.string.pair_title)
            .setView(column)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                pairWith(address.text.toString().trim(), code.text.toString().trim())
            }
            .show()
    }

    private fun pairWith(address: String, code: String) {
        if (address.isEmpty()) {
            toast("Type the PC's address")
            return
        }
        note = "Pairing…"
        showStatus()
        thread(name = "pairing") {
            try {
                val peer = Pairing.pairByAddress(address, code, store.machineId, store.name)
                main.post {
                    runCatching { store.add(peer) }
                        .onFailure { toast(it.message ?: "could not store the pairing") }
                        .onSuccess {
                            store.selectedId = peer.id
                            toast("Paired with ${peer.name}")
                            note = "Paired with ${peer.name}"
                            showStatus()
                        }
                }
            } catch (exc: Pairing.AlreadyPaired) {
                main.post { toast("Already paired with that PC") }
            } catch (exc: Exception) {
                val text = exc.message ?: "pairing failed"
                main.post {
                    toast(text)
                    note = "Pairing failed"
                    showStatus()
                }
            }
        }
    }

    // --- the link ---

    private fun startLink() {
        val peer = store.selected()
        if (peer == null) {
            toast(getString(R.string.no_peer))
            return
        }
        linkError = null
        wasUp = false
        val fresh = BeamerLink(
            peer, store.machineId, store.name,
            onEvent = { kind, detail -> main.post { onLinkEvent(kind, detail) } },
        )
        link = fresh
        fresh.start()
        showStatus()
    }

    private fun stopLink() {
        val current = link
        link = null
        pad.reset()
        views.onscreenPad.reset()
        rumbler.stop()
        if (current != null) {
            if (padAttached) {
                current.sendUnsequenced(PadWire.detach(0))
                padAttached = false
            }
            thread(name = "link-stop") { current.stop() }
        }
        linkError = null
        wasUp = false
        note = ""
        showStatus()
    }

    private fun onLinkEvent(kind: String, detail: JSONObject) {
        when (kind) {
            "welcome" -> {
                if (!detail.optBoolean("gamepad", false)) {
                    toast("The PC's Beamer does not offer 'gamepad'; pad input will not be sent")
                }
            }
            "state" -> {
                if (detail.optString("state") == BeamerLink.STATE_UP) {
                    wasUp = true
                    linkError = null
                    attachPad()
                    // Before any input, as WIRE.md section 3 asks.
                    sendClipboard()
                } else {
                    padAttached = false
                    // The PC sends a stop when it lets go, but a link that died sends nothing.
                    rumbler.stop()
                }
                showStatus()
            }
            "rumble" -> {
                // Whatever is in the user's hands. With a real pad that is the pad's own
                // motors; with the on-screen one the phone is the pad, and Rumbler falls back
                // to this device's vibrator when given no id.
                rumbler.attach(if (mode == Mode.ONSCREEN) null else pad.deviceId)
                rumbler.set(detail.optInt("large"), detail.optInt("small"))
            }
            "clipboard" -> receiveClipboard(detail.optString("text"))
            "error" -> {
                linkError = detail.optString("text").ifEmpty { null }
                showStatus()
            }
        }
    }

    // --- the pad ---

    private fun attachPad() {
        val current = link ?: return
        if (!current.gamepadReady || padAttached) return
        // The on-screen controller is a pad, and there is no InputDevice to find for it.
        if (mode != Mode.ONSCREEN && !PadReader.anyPadAttached()) return
        current.sendUnsequenced(PadWire.attach(0, PadWire.KIND_X360))
        padAttached = true
    }

    private fun sendPad(state: PadWire.State) {
        val current = link ?: return
        if (!current.gamepadReady) return
        if (!padAttached) {
            main.post { attachPad() }
            return
        }
        current.sendPad(PadWire.report(state, 0))
    }

    override fun onGenericMotionEvent(event: MotionEvent): Boolean {
        if (mode == Mode.PAD && pad.onMotion(event)) return true
        return super.onGenericMotionEvent(event)
    }

    // --- keyboard and pad key events ---

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode == KeyEvent.KEYCODE_BACK) return super.dispatchKeyEvent(event)

        if (mode == Mode.PAD && pad.isPad(event)) {
            pad.onKey(event)
            return true
        }

        val current = link
        if (current != null && current.isUp && isTypable(event)) {
            if (sendKey(event)) return true
        }
        return super.dispatchKeyEvent(event)
    }

    /** A hardware keyboard, or a soft keyboard committing characters. */
    private fun isTypable(event: KeyEvent): Boolean {
        if (event.keyCode == KeyEvent.KEYCODE_UNKNOWN && event.characters != null) return true
        val device = event.device ?: return false
        if (device.keyboardType != InputDevice.KEYBOARD_TYPE_ALPHABETIC) return false
        return device.sources and InputDevice.SOURCE_KEYBOARD == InputDevice.SOURCE_KEYBOARD
    }

    private fun sendKey(event: KeyEvent): Boolean {
        val current = link ?: return false

        KeyNames.textOf(event)?.let { typed ->
            // A soft keyboard's committed string goes as `text`, which the PC types as
            // characters rather than as key presses.
            if (event.action == KeyEvent.ACTION_MULTIPLE || event.action == KeyEvent.ACTION_DOWN) {
                current.sendInput(Wire.text(typed))
            }
            return true
        }

        val name = KeyNames.nameOf(event) ?: return false
        when (event.action) {
            KeyEvent.ACTION_DOWN -> current.sendInput(Wire.key(Wire.MSG_KEYDOWN, name))
            KeyEvent.ACTION_UP -> current.sendInput(Wire.key(Wire.MSG_KEYUP, name))
            else -> return false
        }
        return true
    }

    // --- the touchpad ---

    override fun onMove(dx: Int, dy: Int) {
        link?.sendInput(Wire.mouseMove(dx, dy))
    }

    override fun onButton(button: String, down: Boolean) {
        link?.sendInput(Wire.mouseButton(if (down) Wire.MSG_MOUSEDOWN else Wire.MSG_MOUSEUP, button))
    }

    override fun onScroll(dy: Double, dx: Double) {
        link?.sendInput(Wire.scroll(dy, dx, "line"))
    }

    // --- screens ---------------------------------------------------------------------------
    //
    // Three layers over the stream, each with a flag: the home screen, the menu, the settings.
    // showScreens() makes the views agree with the flags, and everything that changes a flag
    // calls it, so there is one place where "what is visible" is decided.

    private fun wireScreens() {
        val home = views.homeScreen
        home.startTile.setOnClickListener { startPlaying() }
        home.settingsButton.setOnClickListener { openSettings() }
        home.helpButton.setOnClickListener { showHelp() }
        home.rowCard.setOnClickListener { if (!cameraOpen) toast(getString(R.string.no_card)) }
        home.rowPeer.setOnClickListener { askToPair() }
        home.rowInput.setOnClickListener { setMode(nextMode(mode)) }
        home.rowLink.setOnClickListener { if (link == null) startPlaying() }
        home.version.text = "v" + runCatching {
            packageManager.getPackageInfo(packageName, 0).versionName
        }.getOrNull().orEmpty()

        views.menuButton.setOnClickListener { setMenu(true) }
        views.streamKeyboard.setOnClickListener { toggleKeyboard() }

        val menu = views.sessionMenu
        menu.root.setOnClickListener { setMenu(false) }          // the dimmed picture beside it
        menu.closeMenu.setOnClickListener { setMenu(false) }
        menu.modeTouch.setOnClickListener { setMode(Mode.TOUCH) }
        menu.modeOnscreen.setOnClickListener { setMode(Mode.ONSCREEN) }
        menu.modeController.setOnClickListener { setMode(Mode.PAD) }
        menu.keyboardRow.setOnClickListener {
            setMenu(false)
            toggleKeyboard()
        }
        menu.soundRow.setOnClickListener { toggleSound() }
        menu.homeRow.setOnClickListener { goHome() }
        menu.disconnectRow.setOnClickListener {
            stopLink()
            goHome()
        }

        val settings = views.settingsScreen
        settings.root.setOnClickListener { closeSettings() }
        settings.closeSettings.setOnClickListener { closeSettings() }
        settings.pairRow.setOnClickListener {
            closeSettings()
            askToPair()
        }
        settings.forgetRow.setOnClickListener { forgetPeer() }
        settings.opacitySeek.setOnSeekBarChangeListener(onSeek { value ->
            val opacity = 0.1f + value / 100f
            store.padOpacity = opacity
            views.onscreenPad.opacity = opacity
        })
        settings.speedSeek.setOnSeekBarChangeListener(onSeek { value ->
            val speed = 0.6f + value / 10f
            store.mouseScale = speed
            views.touchpad.scale = speed
        })

        onBackPressedDispatcher.addCallback(this, backHandler)
    }

    /** Start Playing: pair first if there is nothing to play, otherwise connect and go to it. */
    private fun startPlaying() {
        if (store.selected() == null) {
            askToPair()
            return
        }
        if (link == null) startLink()
        homeShown = false
        menuShown = false
        showScreens()
    }

    /** Back to the home screen. The link stays up: Resume picks the stream back up. */
    private fun goHome() {
        hideKeyboard()
        menuShown = false
        homeShown = true
        showScreens()
    }

    private fun setMenu(open: Boolean) {
        menuShown = open
        if (open) hideKeyboard()
        showScreens()
    }

    private fun openSettings() {
        val settings = views.settingsScreen
        settings.opacitySeek.progress = ((store.padOpacity - 0.1f) * 100f).roundToInt().coerceIn(0, 90)
        settings.speedSeek.progress = ((store.mouseScale - 0.6f) * 10f).roundToInt().coerceIn(0, 40)
        val peer = store.selected()
        settings.forgetRow.visibility = if (peer != null) View.VISIBLE else View.GONE
        settings.forgetLabel.text = getString(R.string.forget_pc, peer?.name.orEmpty())
        settings.aboutText.text = buildString {
            append("CaptureKVM ").append(views.homeScreen.version.text)
            append("\nThis device: ").append(store.name)
            peer?.let { append("\nPaired with: ").append(it.name).append(" (").append(it.host).append(")") }
        }
        settingsShown = true
        showScreens()
    }

    private fun closeSettings() {
        settingsShown = false
        showScreens()
    }

    private fun forgetPeer() {
        val peer = store.selected() ?: return
        MaterialAlertDialogBuilder(this, R.style.Theme_CaptureKVM_Dialog)
            .setTitle(getString(R.string.forget_pc, peer.name))
            .setMessage("To use it again you will need a new six-digit code from the PC.")
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                if (link != null) stopLink()
                store.forget(peer.id)
                store.selectedId = null
                closeSettings()
            }
            .show()
    }

    private fun showHelp() {
        MaterialAlertDialogBuilder(this, R.style.Theme_CaptureKVM_Dialog)
            .setTitle(R.string.help)
            .setMessage(R.string.help_text)
            .setPositiveButton(android.R.string.ok, null)
            .show()
    }

    /** Make the views agree with the three flags. */
    private fun showScreens() {
        val streaming = !homeShown
        views.homeScreen.root.visibility = if (homeShown) View.VISIBLE else View.GONE
        views.menuButton.visibility = if (streaming) View.VISIBLE else View.GONE
        views.streamKeyboard.visibility = if (streaming) View.VISIBLE else View.GONE
        views.sessionMenu.root.visibility = if (streaming && menuShown) View.VISIBLE else View.GONE
        views.settingsScreen.root.visibility = if (settingsShown) View.VISIBLE else View.GONE
        backHandler.isEnabled = settingsShown || menuShown || streaming
        showStatus()
    }

    // --- input modes ---

    private fun nextMode(current: Mode) = when (current) {
        Mode.TOUCH -> Mode.ONSCREEN
        Mode.ONSCREEN -> Mode.PAD
        Mode.PAD -> Mode.TOUCH
    }

    private fun setMode(next: Mode) {
        if (next == mode) return
        mode = next
        store.inputMode = next.key
        applyMode()
        if (next == Mode.PAD && !PadReader.anyPadAttached()) toast(getString(R.string.no_controller))
        showStatus()
    }

    /**
     * Show the one input surface this mode uses and silence the others.
     *
     * Both pads are reset on the way through rather than only the one being left: whichever was
     * holding a button has to let go of it on the PC, and a mode change is exactly when a held
     * button would otherwise be stranded.
     */
    private fun applyMode() {
        views.touchpad.visibility = if (mode == Mode.TOUCH) View.VISIBLE else View.GONE
        views.onscreenPad.visibility = if (mode == Mode.ONSCREEN) View.VISIBLE else View.GONE
        pad.reset()
        views.onscreenPad.reset()
        if (mode != Mode.TOUCH) hideKeyboard()
        if (mode == Mode.ONSCREEN) main.post { attachPad() }
    }

    // --- what the screens say ---

    private fun showStatus() {
        showHome()
        showMenu()
        showStreamStatus()
    }

    /** The connection, in one line: the same words on the home screen and in the menu. */
    private fun linkLine(): String {
        val current = link
            ?: return getString(if (store.selected() == null) R.string.link_off else R.string.link_ready)
        if (current.isUp) {
            val ms = current.rttMs?.let { " · ${it.toInt()} ms" }.orEmpty()
            return getString(R.string.link_good) + ms
        }
        return linkError ?: getString(R.string.link_connecting)
    }

    private fun showHome() {
        val home = views.homeScreen
        val peer = store.selected()
        val current = link

        home.labelCard.text = cardText
        check(home.statusCard, when {
            cameraOpen -> Check.OK
            attached != null -> Check.PENDING
            else -> Check.BAD
        })

        home.labelPeer.text = peer?.name ?: getString(R.string.no_peer)
        check(home.statusPeer, if (peer != null) Check.OK else Check.BAD)

        val padHere = PadReader.anyPadAttached()
        home.iconInput.setImageResource(modeIcon(mode))
        home.labelInput.text = when (mode) {
            Mode.TOUCH -> getString(R.string.mode_touch)
            Mode.ONSCREEN -> getString(R.string.mode_onscreen)
            Mode.PAD -> getString(if (padHere) R.string.controller_found else R.string.no_controller_short)
        }
        check(home.statusInput, if (mode == Mode.PAD && !padHere) Check.BAD else Check.OK)

        home.labelLink.text = linkLine()
        check(home.statusLink, when {
            current == null -> Check.IDLE
            current.isUp -> Check.OK
            linkError != null -> Check.BAD
            else -> Check.PENDING
        })

        when {
            peer == null -> {
                home.startIcon.setImageResource(R.drawable.ic_add)
                home.startLabel.setText(R.string.pair_a_pc)
            }
            current != null -> {
                home.startIcon.setImageResource(R.drawable.ic_play)
                home.startLabel.setText(R.string.resume)
            }
            else -> {
                home.startIcon.setImageResource(R.drawable.ic_play)
                home.startLabel.setText(R.string.start_playing)
            }
        }
    }

    private fun showMenu() {
        val menu = views.sessionMenu
        menu.menuPeer.text = store.selected()?.name ?: getString(R.string.no_peer)
        menu.menuDetail.text = if (note.isEmpty()) linkLine() else linkLine() + "\n" + note

        val padHere = PadReader.anyPadAttached()
        menu.modeControllerLabel.text = if (padHere) getString(R.string.mode_pad)
        else getString(R.string.mode_pad) + " — " + getString(R.string.no_controller_short).lowercase()
        val rows = listOf(
            Triple(Mode.TOUCH, menu.modeTouch, menu.modeTouchIcon to menu.modeTouchLabel),
            Triple(Mode.ONSCREEN, menu.modeOnscreen, menu.modeOnscreenIcon to menu.modeOnscreenLabel),
            Triple(Mode.PAD, menu.modeController, menu.modeControllerIcon to menu.modeControllerLabel),
        )
        for ((rowMode, row, parts) in rows) {
            val chosen = rowMode == mode
            row.isSelected = chosen
            val colour = getColor(if (chosen) R.color.steam_accent else R.color.steam_text)
            parts.first.imageTintList = ColorStateList.valueOf(colour)
            parts.second.setTextColor(colour)
        }

        val playing = audio.isRunning
        menu.soundIcon.setImageResource(if (playing) R.drawable.ic_volume_up else R.drawable.ic_volume_off)
        menu.soundLabel.setText(if (playing) R.string.sound_on else R.string.sound_off)
        menu.disconnectRow.visibility = if (link != null) View.VISIBLE else View.GONE
    }

    /** A line at the top of the stream while there is something wrong with it, and none when
     *  there is not: the point of the stream is that nothing is drawn over the PC. */
    private fun showStreamStatus() {
        val current = link
        val name = store.selected()?.name ?: "the PC"
        val text = when {
            homeShown -> null
            current == null -> getString(R.string.not_streaming)
            !current.isUp && wasUp -> getString(R.string.reconnecting, name)
            !current.isUp -> linkError ?: getString(R.string.connecting_to, name)
            !cameraOpen -> cardText
            else -> null
        }
        views.streamStatus.text = text.orEmpty()
        views.streamStatus.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    private fun check(view: ImageView, state: Check) {
        val (colour, icon) = when (state) {
            Check.OK -> R.color.ok to R.drawable.ic_check
            Check.PENDING -> R.color.warn to R.drawable.ic_more
            Check.BAD -> R.color.bad to R.drawable.ic_close
            Check.IDLE -> R.color.idle to R.drawable.ic_more
        }
        view.backgroundTintList = ColorStateList.valueOf(getColor(colour))
        view.setImageResource(icon)
    }

    private fun modeIcon(of: Mode) = when (of) {
        Mode.TOUCH -> R.drawable.ic_mouse
        Mode.ONSCREEN -> R.drawable.ic_touch
        Mode.PAD -> R.drawable.ic_gamepad
    }

    private fun onSeek(changed: (Int) -> Unit) = object : SeekBar.OnSeekBarChangeListener {
        override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) {
            if (fromUser) changed(value)
        }

        override fun onStartTrackingTouch(bar: SeekBar) = Unit
        override fun onStopTrackingTouch(bar: SeekBar) = Unit
    }

    // --- keyboard and system bars ---

    private fun toggleKeyboard() {
        val manager = getSystemService(InputMethodManager::class.java)
        if (views.keyboardSink.hasFocus()) {
            hideKeyboard()
        } else {
            views.keyboardSink.requestFocus()
            manager.showSoftInput(views.keyboardSink, InputMethodManager.SHOW_IMPLICIT)
        }
    }

    private fun hideKeyboard() {
        val manager = getSystemService(InputMethodManager::class.java)
        manager.hideSoftInputFromWindow(views.keyboardSink.windowToken, 0)
        views.keyboardSink.clearFocus()
        views.root.requestFocus()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // Doing this in onCreate only is the usual Android mistake: the soft keyboard, a dialog
        // and coming back from the launcher each bring Android's bars back, and nothing takes
        // them away again. They are asked to go every time this window is the one in front.
        if (hasFocus) goImmersive()
    }

    /**
     * Android's own bars away. Decided by the API level, not by whether `windowInsetsController`
     * comes back null: it is an Android 11 method, and on 8, 9 and 10 asking for it throws rather
     * than answering null -- from onCreate, so the app used to crash on launch there.
     */
    private fun goImmersive() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.decorView.windowInsetsController?.let { controller ->
                controller.hide(android.view.WindowInsets.Type.systemBars())
                controller.systemBarsBehavior =
                    android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility =
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                    View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
                    View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
        }
    }

    private fun toast(text: String) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show()
    }

    override fun onDestroy() {
        audio.stop()
        stopLink()
        super.onDestroy()
    }
}
