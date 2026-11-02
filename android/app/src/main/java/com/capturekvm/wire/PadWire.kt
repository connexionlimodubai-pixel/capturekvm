package com.capturekvm.wire

import org.json.JSONObject

/**
 * The gamepad messages, which upstream Beamer has no type for. `docs/PAD-PROTOCOL.md` is the
 * contract; the fields are `XUSB_REPORT`'s, so a ViGEmBus responder can hand them straight to
 * `vigem_target_x360_update`.
 *
 * Kept in step with `windows/capturekvm/padwire.py` by hand -- the two are short and the field
 * names are the whole agreement.
 */
object PadWire {

    const val CAP_GAMEPAD = "gamepad"

    /** Offered by a client that can actually make its pad rumble; independent of CAP_GAMEPAD. */
    const val CAP_RUMBLE = "gamepad_rumble"

    const val MSG_ATTACH = "gamepad_attach"
    const val MSG_DETACH = "gamepad_detach"
    const val MSG_REPORT = "gamepad"

    /** The one that travels the other way: the PC telling the client what the game wants. */
    const val MSG_RUMBLE = "gamepad_rumble"

    const val KIND_X360 = "x360"
    const val KIND_DS4 = "ds4"

    const val MAX_PADS = 4

    // xinput.h / XUSB_BUTTON, unchanged so the two ends cannot drift.
    const val BTN_DPAD_UP = 0x0001
    const val BTN_DPAD_DOWN = 0x0002
    const val BTN_DPAD_LEFT = 0x0004
    const val BTN_DPAD_RIGHT = 0x0008
    const val BTN_START = 0x0010
    const val BTN_BACK = 0x0020
    const val BTN_LEFT_THUMB = 0x0040
    const val BTN_RIGHT_THUMB = 0x0080
    const val BTN_LEFT_SHOULDER = 0x0100
    const val BTN_RIGHT_SHOULDER = 0x0200
    const val BTN_GUIDE = 0x0400
    const val BTN_A = 0x1000
    const val BTN_B = 0x2000
    const val BTN_X = 0x4000
    const val BTN_Y = 0x8000

    const val BUTTON_MASK = 0xFFFF
    const val AXIS_MIN = -32768
    const val AXIS_MAX = 32767
    const val TRIGGER_MAX = 255

    /** Motor speeds travel at ViGEmBus's own resolution, one byte each. */
    const val MOTOR_MAX = 255

    /** (pad, large, small) from a rumble, or null when the frame is malformed. */
    fun readRumble(message: org.json.JSONObject): Triple<Int, Int, Int>? {
        if (message.optString("type") != MSG_RUMBLE) return null
        val data = message.optJSONObject("data") ?: return null
        val pad = data.optInt("pad", 0)
        val large = data.optInt("large", 0)
        val small = data.optInt("small", 0)
        if (pad !in 0 until MAX_PADS) return null
        if (large !in 0..MOTOR_MAX || small !in 0..MOTOR_MAX) return null
        return Triple(pad, large, small)
    }

    /** One pad's reading in XUSB units. */
    data class State(
        var buttons: Int = 0,
        var lt: Int = 0,
        var rt: Int = 0,
        var lx: Int = 0,
        var ly: Int = 0,
        var rx: Int = 0,
        var ry: Int = 0,
    ) {
        fun clear() {
            buttons = 0; lt = 0; rt = 0; lx = 0; ly = 0; rx = 0; ry = 0
        }

        fun isNeutral(): Boolean =
            buttons == 0 && lt == 0 && rt == 0 && lx == 0 && ly == 0 && rx == 0 && ry == 0

        fun setButton(mask: Int, down: Boolean) {
            buttons = if (down) buttons or mask else buttons and mask.inv()
        }
    }

    fun attach(pad: Int = 0, kind: String = KIND_X360): JSONObject = JSONObject()
        .put("type", MSG_ATTACH)
        .put("data", JSONObject().put("pad", pad).put("kind", kind))

    fun detach(pad: Int = 0): JSONObject = JSONObject()
        .put("type", MSG_DETACH)
        .put("data", JSONObject().put("pad", pad))

    /** A full report rather than a delta: a dropped frame costs a poll, not a stuck button. */
    fun report(state: State, pad: Int = 0): JSONObject {
        val data = JSONObject()
            .put("pad", pad.coerceIn(0, MAX_PADS - 1))
            .put("buttons", state.buttons and BUTTON_MASK)
            .put("lt", state.lt.coerceIn(0, TRIGGER_MAX))
            .put("rt", state.rt.coerceIn(0, TRIGGER_MAX))
            .put("lx", state.lx.coerceIn(AXIS_MIN, AXIS_MAX))
            .put("ly", state.ly.coerceIn(AXIS_MIN, AXIS_MAX))
            .put("rx", state.rx.coerceIn(AXIS_MIN, AXIS_MAX))
            .put("ry", state.ry.coerceIn(AXIS_MIN, AXIS_MAX))
        return JSONObject().put("type", MSG_REPORT).put("data", data)
    }
}
