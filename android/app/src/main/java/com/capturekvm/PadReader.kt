package com.capturekvm

import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import com.capturekvm.wire.PadWire

/**
 * A pad plugged into the tablet, read through Android's input events and kept in XUSB units so
 * the PC's ViGEmBus target can take the reading unchanged.
 *
 * Two shapes of event arrive. Buttons and the D-pad come as key events; sticks, triggers and
 * (on most pads) the D-pad hat come as motion events on a joystick source. Android's Y axes
 * point down and XUSB's point up, so the vertical ones are negated here rather than anywhere
 * further along.
 *
 * [onChanged] fires only when the reading actually differs, so a stick at rest costs nothing.
 */
class PadReader(private val onChanged: (PadWire.State) -> Unit) {

    val state = PadWire.State()
    private var lastSent = PadWire.State()
    var deviceId: Int? = null; private set

    private val buttons = mapOf(
        KeyEvent.KEYCODE_BUTTON_A to PadWire.BTN_A,
        KeyEvent.KEYCODE_BUTTON_B to PadWire.BTN_B,
        KeyEvent.KEYCODE_BUTTON_X to PadWire.BTN_X,
        KeyEvent.KEYCODE_BUTTON_Y to PadWire.BTN_Y,
        KeyEvent.KEYCODE_BUTTON_L1 to PadWire.BTN_LEFT_SHOULDER,
        KeyEvent.KEYCODE_BUTTON_R1 to PadWire.BTN_RIGHT_SHOULDER,
        KeyEvent.KEYCODE_BUTTON_THUMBL to PadWire.BTN_LEFT_THUMB,
        KeyEvent.KEYCODE_BUTTON_THUMBR to PadWire.BTN_RIGHT_THUMB,
        KeyEvent.KEYCODE_BUTTON_START to PadWire.BTN_START,
        KeyEvent.KEYCODE_BUTTON_SELECT to PadWire.BTN_BACK,
        // KEYCODE_BACK is deliberately absent. Some pads send it for Select, but trapping it
        // would leave no way out of the app; Select reaches the PC, Back leaves.
        KeyEvent.KEYCODE_BUTTON_MODE to PadWire.BTN_GUIDE,
        KeyEvent.KEYCODE_DPAD_UP to PadWire.BTN_DPAD_UP,
        KeyEvent.KEYCODE_DPAD_DOWN to PadWire.BTN_DPAD_DOWN,
        KeyEvent.KEYCODE_DPAD_LEFT to PadWire.BTN_DPAD_LEFT,
        KeyEvent.KEYCODE_DPAD_RIGHT to PadWire.BTN_DPAD_RIGHT,
    )

    /** Whether [event] came from a pad rather than a keyboard or the touchscreen. */
    fun isPad(event: KeyEvent): Boolean = isPadSource(event.source) && buttons.containsKey(event.keyCode)

    fun isPad(event: MotionEvent): Boolean =
        isPadSource(event.source) && event.action == MotionEvent.ACTION_MOVE

    private fun isPadSource(source: Int): Boolean =
        source and InputDevice.SOURCE_GAMEPAD == InputDevice.SOURCE_GAMEPAD ||
            source and InputDevice.SOURCE_JOYSTICK == InputDevice.SOURCE_JOYSTICK ||
            source and InputDevice.SOURCE_DPAD == InputDevice.SOURCE_DPAD

    /** Returns true when the event belonged to a pad and was consumed. */
    fun onKey(event: KeyEvent): Boolean {
        val mask = buttons[event.keyCode] ?: return false
        if (!isPadSource(event.source)) return false
        // L2/R2 arrive as buttons on some pads and as axes on others; the axis path wins when
        // both are present, because it carries how far the trigger was pulled.
        when (event.action) {
            KeyEvent.ACTION_DOWN -> state.setButton(mask, true)
            KeyEvent.ACTION_UP -> state.setButton(mask, false)
            else -> return false
        }
        deviceId = event.deviceId
        publish()
        return true
    }

    fun onMotion(event: MotionEvent): Boolean {
        if (!isPad(event)) return false
        deviceId = event.deviceId

        state.lx = axis(event, MotionEvent.AXIS_X)
        state.ly = -axis(event, MotionEvent.AXIS_Y)
        // Right stick: Z/RZ on Xbox-style pads, RX/RY on a few others. Whichever moves is used.
        val zx = axis(event, MotionEvent.AXIS_Z)
        val zy = axis(event, MotionEvent.AXIS_RZ)
        if (zx != 0 || zy != 0) {
            state.rx = zx
            state.ry = -zy
        } else {
            state.rx = axis(event, MotionEvent.AXIS_RX)
            state.ry = -axis(event, MotionEvent.AXIS_RY)
        }

        state.lt = trigger(event, MotionEvent.AXIS_LTRIGGER, MotionEvent.AXIS_BRAKE)
        state.rt = trigger(event, MotionEvent.AXIS_RTRIGGER, MotionEvent.AXIS_GAS)

        // The hat is how most pads report the D-pad on a joystick source.
        val hatX = event.getAxisValue(MotionEvent.AXIS_HAT_X)
        val hatY = event.getAxisValue(MotionEvent.AXIS_HAT_Y)
        state.setButton(PadWire.BTN_DPAD_LEFT, hatX < -0.5f)
        state.setButton(PadWire.BTN_DPAD_RIGHT, hatX > 0.5f)
        state.setButton(PadWire.BTN_DPAD_UP, hatY < -0.5f)
        state.setButton(PadWire.BTN_DPAD_DOWN, hatY > 0.5f)

        publish()
        return true
    }

    /** Called when the link drops or the pad goes away: nothing may be left held on the PC. */
    fun reset() {
        state.clear()
        publish()
    }

    private fun publish() {
        if (state == lastSent) return
        lastSent = state.copy()
        onChanged(state)
    }

    private fun axis(event: MotionEvent, axis: Int): Int {
        val raw = centred(event, axis)
        return (raw * PadWire.AXIS_MAX).toInt().coerceIn(PadWire.AXIS_MIN, PadWire.AXIS_MAX)
    }

    private fun trigger(event: MotionEvent, first: Int, second: Int): Int {
        val raw = maxOf(event.getAxisValue(first), event.getAxisValue(second))
        return (raw * PadWire.TRIGGER_MAX).toInt().coerceIn(0, PadWire.TRIGGER_MAX)
    }

    /**
     * The axis value with its device's own flat zone removed. A worn stick reports a resting
     * value of a few per cent, and sending that would make the PC's pointer or camera drift.
     */
    private fun centred(event: MotionEvent, axis: Int): Float {
        val value = event.getAxisValue(axis)
        val range = event.device?.getMotionRange(axis, event.source) ?: return value
        val flat = maxOf(range.flat, 0.02f)
        if (kotlin.math.abs(value) <= flat) return 0f
        val span = 1f - flat
        return if (span <= 0f) value else (value - flat * kotlin.math.sign(value)) / span
    }

    companion object {
        /** Whether any pad is attached right now. */
        fun anyPadAttached(): Boolean = InputDevice.getDeviceIds().any { id ->
            val device = InputDevice.getDevice(id) ?: return@any false
            val source = device.sources
            source and InputDevice.SOURCE_GAMEPAD == InputDevice.SOURCE_GAMEPAD ||
                source and InputDevice.SOURCE_JOYSTICK == InputDevice.SOURCE_JOYSTICK
        }
    }
}
