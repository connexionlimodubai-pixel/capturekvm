package com.capturekvm

import android.content.Context
import android.media.AudioAttributes
import android.os.Build
import android.os.CombinedVibration
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import android.view.InputDevice

/**
 * The game's rumble, felt in the hands holding the pad.
 *
 * The PC is the only side that knows when to rumble -- the game runs there -- so this is the one
 * message in the pad extension that travels responder to initiator. What arrives is ViGEmBus's
 * own pair of motor speeds, 0 to 255: `large` is the low-frequency motor, `small` the
 * high-frequency one.
 *
 * The pad's own motors are used where Android exposes them. A controller reports its vibrators
 * through its `InputDevice`, and on API 31 and later a pad with two of them can be driven as
 * two, which is the difference between rumble and a buzz.
 *
 * The phone's own vibrator is the fallback, for the on-screen pad: there the phone *is* the pad,
 * and the hands holding it are the ones the game is trying to reach.
 *
 * Effects are re-issued rather than held: `createOneShot` has a duration, and the PC sends a new
 * value only when it changes, so each effect outlasts the gap to the next one and a stop cancels
 * outright. Amplitude 0 is not a legal one-shot, so zero means cancel rather than a silent
 * effect -- which also makes "stop" the one thing that cannot be mistaken for anything else.
 */
class Rumbler(private val context: Context? = null) {

    companion object {
        private const val TAG = "capturekvm.rumble"

        /** Longer than the gap between updates, so a held rumble does not stutter between them. */
        private const val EFFECT_MS = 400L

        private val GAME_AUDIO = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_GAME)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build()
    }

    private var deviceId: Int? = null
    private var configured = false
    private var manager: VibratorManager? = null
    private var single: Vibrator? = null
    private var pair: Pair<Vibrator, Vibrator>? = null

    @Volatile var lastLarge: Int = 0; private set
    @Volatile var lastSmall: Int = 0; private set

    val available: Boolean get() = manager != null || single != null

    /** Point it at the pad currently being used. Safe to call repeatedly with the same id. */
    fun attach(deviceId: Int?) {
        // `configured` as well as the id: the first call may be attach(null) for the on-screen
        // pad, and comparing ids alone would take that for "already set up" and never look for
        // the phone's own vibrator.
        if (configured && deviceId == this.deviceId) return
        stop()
        this.deviceId = deviceId
        configured = true
        manager = null; single = null; pair = null

        val device = deviceId?.let { InputDevice.getDevice(it) }
        if (device != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val found = device.vibratorManager
                val ids = found.vibratorIds
                // Only when it has one: an empty manager would look available and do nothing,
                // and the phone's vibrator below would never be tried.
                if (ids.isNotEmpty()) manager = found
                if (ids.size >= 2) {
                    // Two motors: drive them as the two the game asked for.
                    pair = found.getVibrator(ids[0]) to found.getVibrator(ids[1])
                }
                Log.i(TAG, "pad $deviceId has ${ids.size} vibrator(s)")
            } else {
                @Suppress("DEPRECATION")
                val legacy = device.vibrator
                if (legacy != null && legacy.hasVibrator()) single = legacy
            }
            if (available) return
        }
        useSystemVibrator()
    }

    /** No pad, or one with no motors: rumble this device instead. */
    private fun useSystemVibrator() {
        val host = context ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val found = host.getSystemService(VibratorManager::class.java) ?: return
            if (found.defaultVibrator.hasVibrator()) manager = found
        } else {
            @Suppress("DEPRECATION")
            val legacy = host.getSystemService(Vibrator::class.java)
            if (legacy != null && legacy.hasVibrator()) single = legacy
        }
        Log.i(TAG, "rumbling this device: ${available}")
    }

    /** What the PC asked for, 0-255 each. */
    fun set(large: Int, small: Int) {
        lastLarge = large.coerceIn(0, 255)
        lastSmall = small.coerceIn(0, 255)
        if (lastLarge == 0 && lastSmall == 0) {
            stop()
            return
        }
        runCatching { play(lastLarge, lastSmall) }
            .onFailure { Log.i(TAG, "rumble failed: ${it.message}") }
    }

    private fun play(large: Int, small: Int) {
        pair?.let { (low, high) ->
            if (large > 0) low.vibrate(oneShot(large), GAME_AUDIO) else low.cancel()
            if (small > 0) high.vibrate(oneShot(small), GAME_AUDIO) else high.cancel()
            return
        }
        // One motor, so the two speeds have to become one. The stronger wins rather than their
        // average: a light high-frequency buzz should not dilute a heavy rumble.
        val combined = maxOf(large, small)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && manager != null) {
            // No attributes here: VibratorManager takes VibrationAttributes, not the
            // AudioAttributes a single Vibrator accepts, and the plain overload is enough.
            manager!!.vibrate(CombinedVibration.createParallel(oneShot(combined)))
            return
        }
        @Suppress("DEPRECATION")
        single?.vibrate(oneShot(combined), GAME_AUDIO)
    }

    private fun oneShot(amplitude: Int): VibrationEffect =
        VibrationEffect.createOneShot(EFFECT_MS, amplitude.coerceIn(1, 255))

    /** Always safe. Called whenever the link ends, so nothing is left buzzing. */
    fun stop() {
        lastLarge = 0
        lastSmall = 0
        runCatching {
            pair?.let { (low, high) -> low.cancel(); high.cancel() }
            manager?.cancel()
            @Suppress("DEPRECATION")
            single?.cancel()
        }
    }
}
