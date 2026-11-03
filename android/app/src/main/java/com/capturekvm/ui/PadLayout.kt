package com.capturekvm.ui

import com.capturekvm.wire.PadWire
import kotlin.math.hypot
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Where every on-screen pad control sits, and what a touch on one means.
 *
 * Separate from [GamepadView] and free of any Android type on purpose: this is the half that can
 * be wrong in ways nothing visible would show -- a stick that reaches only nine tenths of the
 * way, an inverted vertical axis, a dead zone that swallows a slow lean -- and a plain class can
 * be unit-tested on the JVM. The view draws what this works out and does nothing else with it.
 *
 * Geometry follows a real pad rather than packing everything into thumb's reach: sticks in the
 * bottom corners where thumbs rest, the D-pad and the face buttons above them, shoulders along
 * the top. Sizes are in density-independent pixels and shrink together on a short screen, so a
 * phone in landscape gets the same layout as a tablet rather than a cramped different one.
 */
class PadLayout(val width: Float, val height: Float, density: Float) {

    enum class Control {
        LEFT_STICK, RIGHT_STICK, DPAD,
        A, B, X, Y,
        LB, RB, LT, RT,
        BACK, START, GUIDE,
    }

    data class Circle(val cx: Float, val cy: Float, val radius: Float) {
        fun holds(x: Float, y: Float, slack: Float = 1f): Boolean =
            hypot(x - cx, y - cy) <= radius * slack
    }

    data class Pill(val left: Float, val top: Float, val right: Float, val bottom: Float) {
        val cx get() = (left + right) / 2f
        val cy get() = (top + bottom) / 2f
        fun holds(x: Float, y: Float): Boolean = x in left..right && y in top..bottom
    }

    /** A stick leaned less than this is treated as untouched; a thumb resting is not an input. */
    private val deadZone = 0.08f

    /** How far from centre a thumb must be before the D-pad reads a direction. Low enough that
     *  diagonals come naturally from the two axes crossing it at once. */
    private val dpadThreshold = 0.30f

    // Everything scales with the shorter edge, so the pad stays in proportion on a phone held in
    // landscape (around 360dp tall) and on a tablet (around 800dp) alike.
    private val scale = min(1f, height / (380f * density)).coerceAtLeast(0.62f)
    private val dp = density * scale

    val stickRadius = 58f * dp
    val knobRadius = 26f * dp
    val dpadRadius = 44f * dp
    val faceRadius = 23f * dp

    private val margin = 16f * dp
    private val gap = 8f * dp
    private val pillWidth = 60f * dp
    private val pillHeight = 34f * dp
    private val smallWidth = 48f * dp
    private val smallHeight = 26f * dp

    val leftStick = Circle(margin + stickRadius, height - margin - stickRadius, stickRadius)
    val rightStick = Circle(width - margin - stickRadius, height - margin - stickRadius, stickRadius)
    val dpad = Circle(leftStick.cx, leftStick.cy - stickRadius - gap - dpadRadius, dpadRadius)

    /**
     * A/B/X/Y in their Xbox places: A at the bottom, B right, X left, Y top.
     *
     * The diamond is placed by its own outer edge, not its centre. Measuring from the centre
     * put A's bottom a button's radius *below* the right stick's top edge -- the two drawn over
     * each other, and a thumb pushing the stick up landing on A. Every number was right and the
     * arithmetic tests passed; it took drawing the thing to see it
     * (PadLayoutPreviewTest), which is why that file exists.
     */
    private val faceSpread = faceRadius * 2.0f
    private val faceReach = faceSpread + faceRadius          // centre to the far side of a button
    private val faceCentre = Circle(
        // Held off the right edge as well as off the stick: on a narrow screen the stick's own
        // centre would put B half over the edge.
        min(rightStick.cx, width - margin - faceReach),
        rightStick.cy - stickRadius - gap - faceReach,
        faceSpread,
    )
    val faces: Map<Control, Circle> = mapOf(
        Control.A to Circle(faceCentre.cx, faceCentre.cy + faceSpread, faceRadius),
        Control.B to Circle(faceCentre.cx + faceSpread, faceCentre.cy, faceRadius),
        Control.X to Circle(faceCentre.cx - faceSpread, faceCentre.cy, faceRadius),
        Control.Y to Circle(faceCentre.cx, faceCentre.cy - faceSpread, faceRadius),
    )

    val pills: Map<Control, Pill> = buildMap {
        put(Control.LT, Pill(margin, margin, margin + pillWidth, margin + pillHeight))
        put(Control.LB, Pill(margin + pillWidth + gap, margin,
                             margin + pillWidth * 2 + gap, margin + pillHeight))
        put(Control.RT, Pill(width - margin - pillWidth, margin, width - margin, margin + pillHeight))
        put(Control.RB, Pill(width - margin - pillWidth * 2 - gap, margin,
                             width - margin - pillWidth - gap, margin + pillHeight))
        // Back, Guide and Start along the middle of the top edge, in that order.
        val row = width / 2f - (smallWidth * 3 + gap * 2) / 2f
        put(Control.BACK, Pill(row, margin, row + smallWidth, margin + smallHeight))
        put(Control.GUIDE, Pill(row + smallWidth + gap, margin,
                                row + smallWidth * 2 + gap, margin + smallHeight))
        put(Control.START, Pill(row + (smallWidth + gap) * 2, margin,
                                row + smallWidth * 3 + gap * 2, margin + smallHeight))
    }

    /** The control under a finger, or null for a touch that belongs to nothing. */
    fun hit(x: Float, y: Float): Control? {
        pills.forEach { (control, pill) -> if (pill.holds(x, y)) return control }
        faces.forEach { (control, circle) -> if (circle.holds(x, y, SLACK)) return control }
        if (dpad.holds(x, y, SLACK)) return Control.DPAD
        // Sticks last and widest: they are the largest targets and the ones a thumb lands on
        // without looking, so anything near one that is not something else is one.
        if (leftStick.holds(x, y, STICK_SLACK)) return Control.LEFT_STICK
        if (rightStick.holds(x, y, STICK_SLACK)) return Control.RIGHT_STICK
        return null
    }

    fun circleFor(control: Control): Circle? = when (control) {
        Control.LEFT_STICK -> leftStick
        Control.RIGHT_STICK -> rightStick
        Control.DPAD -> dpad
        else -> faces[control]
    }

    /**
     * A touch on a stick, as the two XUSB axes.
     *
     * The lean is measured from the zone's centre and clamped at its edge, so sliding a thumb off
     * the circle holds full deflection rather than snapping back. Android's y grows downward and
     * XUSB's grows up, so the vertical one is negated here -- the same correction PadReader makes
     * for a real pad, in the one place each.
     */
    fun stick(control: Control, x: Float, y: Float): Pair<Int, Int> {
        val circle = circleFor(control) ?: return 0 to 0
        var nx = (x - circle.cx) / circle.radius
        var ny = (y - circle.cy) / circle.radius
        val reach = hypot(nx, ny)
        if (reach < deadZone) return 0 to 0
        if (reach > 1f) {
            nx /= reach
            ny /= reach
        }
        return axis(nx) to axis(-ny)
    }

    /** Where to draw a stick's knob: the touch, held inside the zone. */
    fun knob(control: Control, x: Float, y: Float): Pair<Float, Float> {
        val circle = circleFor(control) ?: return x to y
        val dx = x - circle.cx
        val dy = y - circle.cy
        val reach = hypot(dx, dy)
        val limit = circle.radius - knobRadius * 0.5f
        if (reach <= limit || reach == 0f) return x to y
        return circle.cx + dx / reach * limit to circle.cy + dy / reach * limit
    }

    /** The D-pad directions a touch is holding down, as XUSB button bits. */
    fun dpadMask(x: Float, y: Float): Int {
        val threshold = dpad.radius * dpadThreshold
        var mask = 0
        if (x - dpad.cx < -threshold) mask = mask or PadWire.BTN_DPAD_LEFT
        if (x - dpad.cx > threshold) mask = mask or PadWire.BTN_DPAD_RIGHT
        if (y - dpad.cy < -threshold) mask = mask or PadWire.BTN_DPAD_UP
        if (y - dpad.cy > threshold) mask = mask or PadWire.BTN_DPAD_DOWN
        return mask
    }

    /** Rounded, not truncated: truncation biases every reading toward zero, and a thumb held
     *  against the rim came out one short of full deflection because (cx + r) - cx is not
     *  exactly r in float arithmetic. */
    private fun axis(value: Float): Int =
        (value * PadWire.AXIS_MAX).roundToInt().coerceIn(PadWire.AXIS_MIN, PadWire.AXIS_MAX)

    companion object {
        private const val SLACK = 1.15f
        private const val STICK_SLACK = 1.25f

        /** The XUSB button a control stands for, or null for the sticks, the D-pad and the
         *  triggers -- which are not buttons and are read as axes instead. */
        fun buttonFor(control: Control): Int? = when (control) {
            Control.A -> PadWire.BTN_A
            Control.B -> PadWire.BTN_B
            Control.X -> PadWire.BTN_X
            Control.Y -> PadWire.BTN_Y
            Control.LB -> PadWire.BTN_LEFT_SHOULDER
            Control.RB -> PadWire.BTN_RIGHT_SHOULDER
            Control.BACK -> PadWire.BTN_BACK
            Control.START -> PadWire.BTN_START
            Control.GUIDE -> PadWire.BTN_GUIDE
            else -> null
        }

        fun label(control: Control): String = when (control) {
            Control.BACK -> "□□"       // the two-rectangle View glyph
            Control.START -> "☰"
            Control.GUIDE -> "◉"
            Control.LEFT_STICK, Control.RIGHT_STICK, Control.DPAD -> ""
            else -> control.name
        }
    }
}
