package com.capturekvm.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import com.capturekvm.wire.PadWire

/**
 * A controller drawn over the picture, for a phone with nothing plugged into it.
 *
 * Everything it sends is what [com.capturekvm.PadReader] sends for a real pad -- the same
 * [PadWire.State] in XUSB units, down the same link, into the same virtual Xbox pad on the PC --
 * so the PC cannot tell the two apart and nothing new had to be put on the wire for this.
 *
 * [PadLayout] owns where the controls are and what a touch on one means; this class draws them
 * and follows fingers. Multi-touch is the whole point of a pad, so each pointer id is tracked to
 * the control it landed on and keeps it until that finger lifts: a thumb on the left stick and a
 * thumb on A are two independent things, and a stick must not be stolen by a finger that strays
 * over it.
 *
 * Triggers are all or nothing. A touchscreen has no way to say "half pulled" without giving up
 * a slider's worth of room, and a digital trigger that reads 255 is right for every game that
 * treats LT and RT as buttons -- which, on a phone, is most of why anyone would use this.
 */
class GamepadView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    fun interface Listener {
        fun onPad(state: PadWire.State)
    }

    var listener: Listener? = null

    /** How solid the controls look over the picture. The pad is furniture, not the view. */
    var opacity: Float = 0.42f
        set(value) {
            field = value.coerceIn(0.1f, 1f)
            invalidate()
        }

    val state = PadWire.State()
    private var lastSent = PadWire.State()

    private var pad: PadLayout? = null
    private val pointers = HashMap<Int, PadLayout.Control>()
    private val knobs = HashMap<PadLayout.Control, FloatArray>()
    private val pressed = HashSet<PadLayout.Control>()

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val edge = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val ink = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }

    private val scratch = RectF()

    init {
        isFocusable = false
        setWillNotDraw(false)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        pad = if (w > 0 && h > 0) PadLayout(w.toFloat(), h.toFloat(), resources.displayMetrics.density)
        else null
        edge.strokeWidth = 2f * resources.displayMetrics.density
        knobs.clear()
    }

    /** Everything up. Called when the mode changes or the link drops, so the PC is never left
     *  holding a button this view stopped drawing. */
    fun reset() {
        pointers.clear()
        knobs.clear()
        pressed.clear()
        state.clear()
        publish()
        invalidate()
    }

    // --- touch ---

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val layout = pad ?: return false
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> {
                val index = event.actionIndex
                grab(layout, event.getPointerId(index), event.getX(index), event.getY(index))
            }
            MotionEvent.ACTION_MOVE -> {
                for (index in 0 until event.pointerCount) {
                    val control = pointers[event.getPointerId(index)] ?: continue
                    drag(layout, control, event.getX(index), event.getY(index))
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> {
                release(event.getPointerId(event.actionIndex))
                if (event.actionMasked == MotionEvent.ACTION_UP) performClick()
            }
            MotionEvent.ACTION_CANCEL -> reset()
            else -> return false
        }
        publish()
        invalidate()
        return true
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    private fun grab(layout: PadLayout, pointerId: Int, x: Float, y: Float) {
        val control = layout.hit(x, y) ?: return
        // One finger per control: a second finger landing on a stick already held would fight
        // the first for it, and the reading would flick between the two.
        if (pointers.containsValue(control)) return
        pointers[pointerId] = control
        pressed.add(control)
        drag(layout, control, x, y)
    }

    private fun drag(layout: PadLayout, control: PadLayout.Control, x: Float, y: Float) {
        when (control) {
            PadLayout.Control.LEFT_STICK, PadLayout.Control.RIGHT_STICK -> {
                val (ax, ay) = layout.stick(control, x, y)
                if (control == PadLayout.Control.LEFT_STICK) {
                    state.lx = ax; state.ly = ay
                } else {
                    state.rx = ax; state.ry = ay
                }
                val (kx, ky) = layout.knob(control, x, y)
                knobs[control] = floatArrayOf(kx, ky)
            }
            PadLayout.Control.DPAD -> {
                state.buttons = state.buttons and DPAD_BITS.inv() or layout.dpadMask(x, y)
            }
            PadLayout.Control.LT -> state.lt = PadWire.TRIGGER_MAX
            PadLayout.Control.RT -> state.rt = PadWire.TRIGGER_MAX
            else -> PadLayout.buttonFor(control)?.let { state.setButton(it, true) }
        }
    }

    private fun release(pointerId: Int) {
        val control = pointers.remove(pointerId) ?: return
        pressed.remove(control)
        when (control) {
            PadLayout.Control.LEFT_STICK -> { state.lx = 0; state.ly = 0; knobs.remove(control) }
            PadLayout.Control.RIGHT_STICK -> { state.rx = 0; state.ry = 0; knobs.remove(control) }
            PadLayout.Control.DPAD -> state.buttons = state.buttons and DPAD_BITS.inv()
            PadLayout.Control.LT -> state.lt = 0
            PadLayout.Control.RT -> state.rt = 0
            else -> PadLayout.buttonFor(control)?.let { state.setButton(it, false) }
        }
    }

    private fun publish() {
        if (state == lastSent) return
        lastSent = state.copy()
        listener?.onPad(state)
    }

    // --- drawing ---

    override fun onDraw(canvas: Canvas) {
        val layout = pad ?: return
        ink.textSize = layout.faceRadius * 0.82f

        for (stick in listOf(PadLayout.Control.LEFT_STICK, PadLayout.Control.RIGHT_STICK)) {
            val circle = layout.circleFor(stick) ?: continue
            paint(stick, solid = false)
            canvas.drawCircle(circle.cx, circle.cy, circle.radius, fill)
            canvas.drawCircle(circle.cx, circle.cy, circle.radius, edge)
            val knob = knobs[stick]
            val kx = knob?.get(0) ?: circle.cx
            val ky = knob?.get(1) ?: circle.cy
            paint(stick, solid = true)
            canvas.drawCircle(kx, ky, layout.knobRadius, fill)
            canvas.drawCircle(kx, ky, layout.knobRadius, edge)
        }

        drawDpad(canvas, layout)

        layout.faces.forEach { (control, circle) ->
            paint(control, solid = control in pressed)
            canvas.drawCircle(circle.cx, circle.cy, circle.radius, fill)
            canvas.drawCircle(circle.cx, circle.cy, circle.radius, edge)
            label(canvas, PadLayout.label(control), circle.cx, circle.cy)
        }

        layout.pills.forEach { (control, pill) ->
            paint(control, solid = control in pressed)
            scratch.set(pill.left, pill.top, pill.right, pill.bottom)
            val round = (pill.bottom - pill.top) / 2f
            canvas.drawRoundRect(scratch, round, round, fill)
            canvas.drawRoundRect(scratch, round, round, edge)
            label(canvas, PadLayout.label(control), pill.cx, pill.cy)
        }
    }

    private fun drawDpad(canvas: Canvas, layout: PadLayout) {
        val circle = layout.dpad
        paint(PadLayout.Control.DPAD, solid = false)
        canvas.drawCircle(circle.cx, circle.cy, circle.radius, fill)
        canvas.drawCircle(circle.cx, circle.cy, circle.radius, edge)

        // A cross of four arms, each lit on its own, so a diagonal shows as two.
        val arm = circle.radius * 0.72f
        val thick = circle.radius * 0.30f
        val directions = listOf(
            PadWire.BTN_DPAD_UP to floatArrayOf(-thick, -arm, thick, -thick),
            PadWire.BTN_DPAD_DOWN to floatArrayOf(-thick, thick, thick, arm),
            PadWire.BTN_DPAD_LEFT to floatArrayOf(-arm, -thick, -thick, thick),
            PadWire.BTN_DPAD_RIGHT to floatArrayOf(thick, -thick, arm, thick),
        )
        for ((bit, box) in directions) {
            val lit = state.buttons and bit != 0
            fill.color = tint(lit)
            fill.alpha = alpha(if (lit) 0.95f else 0.55f)
            scratch.set(circle.cx + box[0], circle.cy + box[1], circle.cx + box[2], circle.cy + box[3])
            canvas.drawRoundRect(scratch, thick * 0.4f, thick * 0.4f, fill)
        }
    }

    private fun label(canvas: Canvas, text: String, cx: Float, cy: Float) {
        if (text.isEmpty()) return
        // Centred on the glyph's own middle, not on the baseline Canvas would otherwise use.
        val middle = (ink.descent() + ink.ascent()) / 2f
        canvas.drawText(text, cx, cy - middle, ink)
    }

    private fun paint(control: PadLayout.Control, solid: Boolean) {
        val lit = control in pressed
        fill.color = tint(lit)
        fill.alpha = alpha(if (solid) (if (lit) 0.95f else 0.60f) else 0.30f)
        edge.color = tint(lit)
        edge.alpha = alpha(if (lit) 1f else 0.75f)
        ink.color = Color.WHITE
        ink.alpha = alpha(0.95f)
    }

    private fun tint(lit: Boolean): Int = if (lit) ACCENT else Color.WHITE

    private fun alpha(of: Float): Int = (255 * opacity * of).toInt().coerceIn(0, 255)

    private companion object {
        /** The app's accent, so a pressed control looks like the rest of the interface. */
        const val ACCENT = 0xFF5BD1C7.toInt()

        val DPAD_BITS = PadWire.BTN_DPAD_UP or PadWire.BTN_DPAD_DOWN or
            PadWire.BTN_DPAD_LEFT or PadWire.BTN_DPAD_RIGHT
    }
}
