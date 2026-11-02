package com.capturekvm

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.pm.PackageManager
import android.hardware.usb.UsbDevice
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
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.capturekvm.databinding.ActivityMainBinding
import com.capturekvm.ui.TouchpadView
import com.capturekvm.wire.BeamerLink
import com.capturekvm.wire.KeyNames
import com.capturekvm.wire.PadWire
import com.capturekvm.wire.Pairing
import com.capturekvm.wire.Wire
import com.herohan.uvcapp.CameraHelper
import com.herohan.uvcapp.ICameraHelper
import com.serenegiant.usb.Size
import kotlin.concurrent.thread
import org.json.JSONObject

/**
 * The one screen: the card's picture, and whatever is driving the PC.
 *
 * The video never goes near the network. It comes in over USB from the capture card and is drawn
 * straight to a surface, so what you see is as current as the card can make it. Only key presses,
 * pointer movement and pad reports travel, over the Beamer link to the PC.
 *
 * Two input modes, because one set of touches cannot mean two things: Touchpad sends mouse and
 * keyboard, Gamepad sends pad reports and leaves the touchscreen alone.
 */
class MainActivity : AppCompatActivity(), ICameraHelper.StateCallback, TouchpadView.Listener {

    private enum class Mode { TOUCH, PAD }

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

    private var cardText = ""
    private var linkText = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        views = ActivityMainBinding.inflate(layoutInflater)
        setContentView(views.root)
        store = PeerStore(this)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        goImmersive()

        views.touchpad.listener = this
        views.touchpad.scale = store.mouseScale

        views.pairButton.setOnClickListener { askToPair() }
        views.linkButton.setOnClickListener { toggleLink() }
        views.modeButton.setOnClickListener { toggleMode() }
        views.keyboardButton.setOnClickListener { toggleKeyboard() }
        views.soundButton.setOnClickListener { toggleSound() }

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

        cardText = getString(R.string.no_card)
        showStatus()
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
        cardText = "card attached"
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
            cardText = "card ${size.width}x${size.height} @${size.fps}"
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
            views.soundButton.setText(R.string.sound_on)
            showStatus()
            return
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            // Android has no "record this USB device only" permission, so playing the card's
            // sound needs the microphone one. Nothing is stored or sent; see the manifest.
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_AUDIO)
            return
        }
        if (audio.start()) views.soundButton.setText(R.string.sound_off)
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
        if (current.sendClipboard(text)) linkText = "clipboard sent (${text.length} characters)"
        showStatus()
    }

    private fun receiveClipboard(text: String) {
        val manager = getSystemService(ClipboardManager::class.java) ?: return
        runCatching { manager.setPrimaryClip(ClipData.newPlainText("CaptureKVM", text)) }
            .onSuccess {
                linkText = "clipboard from the PC (${text.length} characters)"
                showStatus()
            }
    }

    // --- pairing ---

    private fun askToPair() {
        val frame = layoutInflater.inflate(android.R.layout.simple_list_item_2, null)
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
            setPadding(48, 24, 48, 0)
            addView(address)
            addView(code)
            addView(android.widget.TextView(this@MainActivity).apply {
                text = getString(R.string.pair_hint)
                textSize = 12f
                setPadding(0, 24, 0, 0)
            })
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.pair_title)
            .setView(column)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                pairWith(address.text.toString().trim(), code.text.toString().trim())
            }
            .show()
        frame.visibility = View.GONE
    }

    private fun pairWith(address: String, code: String) {
        if (address.isEmpty()) {
            toast("Type the PC's address")
            return
        }
        linkText = "pairing..."
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
                            linkText = "paired with ${peer.name}"
                            showStatus()
                        }
                }
            } catch (exc: Pairing.AlreadyPaired) {
                main.post { toast("Already paired with that PC") }
            } catch (exc: Exception) {
                val text = exc.message ?: "pairing failed"
                main.post {
                    toast(text)
                    linkText = "pairing failed"
                    showStatus()
                }
            }
        }
    }

    // --- the link ---

    private fun toggleLink() {
        if (link != null) stopLink() else startLink()
    }

    private fun startLink() {
        val peer = store.selected()
        if (peer == null) {
            toast(getString(R.string.no_peer))
            return
        }
        val fresh = BeamerLink(
            peer, store.machineId, store.name,
            onEvent = { kind, detail -> main.post { onLinkEvent(kind, detail) } },
        )
        link = fresh
        fresh.start()
        views.linkButton.setText(R.string.disconnect)
    }

    private fun stopLink() {
        val current = link
        link = null
        pad.reset()
        if (current != null) {
            if (padAttached) {
                current.sendUnsequenced(PadWire.detach(0))
                padAttached = false
            }
            thread(name = "link-stop") { current.stop() }
        }
        views.linkButton.setText(R.string.connect)
        linkText = "link off"
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
                val state = detail.optString("state")
                linkText = "link $state"
                if (state == BeamerLink.STATE_UP) {
                    attachPad()
                    // Before any input, as WIRE.md section 3 asks.
                    sendClipboard()
                } else {
                    padAttached = false
                }
                showStatus()
            }
            "clipboard" -> receiveClipboard(detail.optString("text"))
            "error" -> {
                linkText = detail.optString("text")
                showStatus()
            }
        }
    }

    // --- the pad ---

    private fun attachPad() {
        val current = link ?: return
        if (!current.gamepadReady || padAttached) return
        if (!PadReader.anyPadAttached()) return
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

    // --- chrome ---

    private fun toggleMode() {
        mode = if (mode == Mode.TOUCH) Mode.PAD else Mode.TOUCH
        views.modeButton.setText(if (mode == Mode.TOUCH) R.string.mode_touch else R.string.mode_pad)
        // In pad mode the touch overlay gets out of the way, so a stray thumb cannot move the
        // PC's pointer in the middle of a game.
        views.touchpad.visibility = if (mode == Mode.TOUCH) View.VISIBLE else View.GONE
        if (mode == Mode.TOUCH) pad.reset() else hideKeyboard()
    }

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

    private fun goImmersive() {
        val controller = window.decorView.windowInsetsController
        @Suppress("DEPRECATION")
        if (controller != null) {
            controller.hide(android.view.WindowInsets.Type.systemBars())
            controller.systemBarsBehavior =
                android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        } else {
            window.decorView.systemUiVisibility =
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                    View.SYSTEM_UI_FLAG_FULLSCREEN
        }
    }

    private fun showStatus() {
        val pieces = mutableListOf(cardText)
        if (linkText.isNotEmpty()) pieces.add(linkText)
        link?.rttMs?.let { pieces.add("${it.toInt()} ms") }
        if (padAttached) pieces.add("pad -> PC")
        audio.format?.let { pieces.add("sound $it") }
        views.status.text = pieces.joinToString("   ")
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
