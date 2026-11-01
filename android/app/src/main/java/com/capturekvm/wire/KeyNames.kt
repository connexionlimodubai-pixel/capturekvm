package com.capturekvm.wire

import android.view.KeyEvent

/**
 * Android key codes to WIRE.md section 7's names.
 *
 * The two rules that matter: a character key travels as the character it types, with Shift
 * applied, except when Control, Alt or Meta is held, when it travels unshifted so a shortcut
 * lands on the same letter whatever the layout. Everything else travels as a name.
 *
 * A soft keyboard gives `KEYCODE_UNKNOWN` with the characters in the event, which is why
 * [textOf] exists: those go as a `text` message rather than as keys.
 */
object KeyNames {

    private val NAMED = mapOf(
        KeyEvent.KEYCODE_SHIFT_LEFT to "shift",
        KeyEvent.KEYCODE_SHIFT_RIGHT to "shift_r",
        KeyEvent.KEYCODE_CTRL_LEFT to "ctrl",
        KeyEvent.KEYCODE_CTRL_RIGHT to "ctrl_r",
        KeyEvent.KEYCODE_ALT_LEFT to "alt",
        KeyEvent.KEYCODE_ALT_RIGHT to "alt_r",
        KeyEvent.KEYCODE_META_LEFT to "cmd",
        KeyEvent.KEYCODE_META_RIGHT to "cmd_r",
        KeyEvent.KEYCODE_CAPS_LOCK to "caps_lock",
        KeyEvent.KEYCODE_ENTER to "enter",
        KeyEvent.KEYCODE_NUMPAD_ENTER to "numpad_enter",
        KeyEvent.KEYCODE_DEL to "backspace",
        KeyEvent.KEYCODE_FORWARD_DEL to "delete",
        KeyEvent.KEYCODE_TAB to "tab",
        KeyEvent.KEYCODE_ESCAPE to "esc",
        KeyEvent.KEYCODE_SPACE to "space",
        KeyEvent.KEYCODE_INSERT to "insert",
        KeyEvent.KEYCODE_MOVE_HOME to "home",
        KeyEvent.KEYCODE_MOVE_END to "end",
        KeyEvent.KEYCODE_PAGE_UP to "page_up",
        KeyEvent.KEYCODE_PAGE_DOWN to "page_down",
        KeyEvent.KEYCODE_DPAD_LEFT to "left",
        KeyEvent.KEYCODE_DPAD_RIGHT to "right",
        KeyEvent.KEYCODE_DPAD_UP to "up",
        KeyEvent.KEYCODE_DPAD_DOWN to "down",
        KeyEvent.KEYCODE_MENU to "menu",
        KeyEvent.KEYCODE_SYSRQ to "print_screen",
        KeyEvent.KEYCODE_BREAK to "pause",
        KeyEvent.KEYCODE_NUM_LOCK to "num_lock",
        KeyEvent.KEYCODE_SCROLL_LOCK to "scroll_lock",
        KeyEvent.KEYCODE_VOLUME_MUTE to "volume_mute",
        KeyEvent.KEYCODE_VOLUME_DOWN to "volume_down",
        KeyEvent.KEYCODE_VOLUME_UP to "volume_up",
        KeyEvent.KEYCODE_MEDIA_NEXT to "media_next",
        KeyEvent.KEYCODE_MEDIA_PREVIOUS to "media_prev",
        KeyEvent.KEYCODE_MEDIA_STOP to "media_stop",
        KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE to "media_play_pause",
        KeyEvent.KEYCODE_SEARCH to "search",
        KeyEvent.KEYCODE_CALCULATOR to "calculator",
        KeyEvent.KEYCODE_ENVELOPE to "mail",
    ) + (1..24).associate { index ->
        (KeyEvent.KEYCODE_F1 + index - 1) to "f$index"
    } + (0..9).associate { digit ->
        (KeyEvent.KEYCODE_NUMPAD_0 + digit) to "numpad_$digit"
    } + mapOf(
        KeyEvent.KEYCODE_NUMPAD_ADD to "numpad_add",
        KeyEvent.KEYCODE_NUMPAD_SUBTRACT to "numpad_subtract",
        KeyEvent.KEYCODE_NUMPAD_MULTIPLY to "numpad_multiply",
        KeyEvent.KEYCODE_NUMPAD_DIVIDE to "numpad_divide",
        KeyEvent.KEYCODE_NUMPAD_DOT to "numpad_decimal",
        KeyEvent.KEYCODE_NUMPAD_COMMA to "numpad_separator",
        KeyEvent.KEYCODE_NUMPAD_EQUALS to "numpad_equal",
    )

    /** The wire name for [event], or null for a key not worth sending. */
    fun nameOf(event: KeyEvent): String? {
        NAMED[event.keyCode]?.let { return it }

        val commanded = event.isCtrlPressed || event.isAltPressed || event.isMetaPressed
        if (commanded) {
            // Unshifted, so Ctrl+Shift+Z is ctrl + shift + "z".
            val plain = event.getUnicodeChar(0)
            if (plain != 0) return Character.toString(plain).lowercase()
        }
        val typed = event.unicodeChar
        if (typed != 0) {
            val character = Character.toString(typed)
            if (character.isNotEmpty() && character[0].isDefined() && !character[0].isISOControl()) return character
        }
        val plain = event.getUnicodeChar(0)
        if (plain != 0) return Character.toString(plain).lowercase()
        return null
    }

    /**
     * The characters a soft keyboard committed, for a `text` message. Android reports those as
     * `KEYCODE_UNKNOWN` with the string in the event, and typing them as characters is what
     * WIRE.md says a `text` message is for.
     */
    fun textOf(event: KeyEvent): String? {
        if (event.keyCode != KeyEvent.KEYCODE_UNKNOWN) return null
        val characters = event.characters ?: return null
        return characters.ifEmpty { null }
    }

    /** Modifiers to hold around a synthetic key, in the order they must be pressed. */
    fun modifiersOf(event: KeyEvent): List<String> = buildList {
        if (event.isCtrlPressed) add("ctrl")
        if (event.isAltPressed) add("alt")
        if (event.isShiftPressed) add("shift")
        if (event.isMetaPressed) add("cmd")
    }
}
