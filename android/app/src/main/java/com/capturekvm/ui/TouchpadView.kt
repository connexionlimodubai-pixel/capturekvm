package com.capturekvm.ui

import android.content.Context
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import kotlin.math.abs
import kotlin.math.hypot

/**
 * The picture, used as a trackpad.
 *
 * A trackpad rather than a touchscreen: the PC is driven by relative movement (WIRE.md has no
 * absolute pointer message), and a tablet has no idea where the PC's cursor currently is, so
 * tapping where you want it to go cannot work. Dragging moves it from wherever it was, which is
 * the same bargain a laptop trackpad makes.
 *
 * One finger moves and taps as the left button, two fingers scroll, a long press is the right
 * button, and a drag that starts within [holdSlop] of a tap becomes a press-and-drag.
 */
class TouchpadView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    interface Listener {
        fun onMove(dx: Int, dy: Int)
        fun onButton(button: String, down: Boolean)
        fun onScroll(dy: Double, dx: Double)
    }

    var listener: Listener? = null
    var scale: Float = 1.6f
    var naturalScroll: Boolean = true

    private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop
    private val tapTimeout = ViewConfiguration.getTapTimeout().toLong()
    private val longPressTimeout = ViewConfiguration.getLongPressTimeout().toLong()
    private val holdSlop = touchSlop * 2

    private var lastX = 0f
    private var lastY = 0f
    private var downX = 0f
    private var downY = 0f
    private var downAt = 0L
    private var moved = false
    private var scrolling = false
    private var dragging = false
    private var remainderX = 0f
    private var remainderY = 0f

    private var scrollLastY = 0f
    private var scrollLastX = 0f

    private val longPress = Runnable {
        if (!moved && !scrolling) {
            listener?.onButton("right", true)
            listener?.onButton("right", false)
            moved = true   // so the finger lifting does not also send a left click
        }
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                lastX = event.x; lastY = event.y
                downX = event.x; downY = event.y
                downAt = System.currentTimeMillis()
                moved = false; scrolling = false; dragging = false
                remainderX = 0f; remainderY = 0f
                postDelayed(longPress, longPressTimeout)
            }

            MotionEvent.ACTION_POINTER_DOWN -> {
                removeCallbacks(longPress)
                if (event.pointerCount == 2) {
                    scrolling = true
                    scrollLastY = midY(event)
                    scrollLastX = midX(event)
                    if (dragging) {
                        listener?.onButton("left", false)
                        dragging = false
                    }
                }
            }

            MotionEvent.ACTION_MOVE -> {
                if (scrolling && event.pointerCount >= 2) {
                    val y = midY(event)
                    val x = midX(event)
                    val stepY = (y - scrollLastY) / 24.0
                    val stepX = (x - scrollLastX) / 24.0
                    scrollLastY = y; scrollLastX = x
                    if (abs(stepY) > 0.01 || abs(stepX) > 0.01) {
                        // Positive dy moves the view up, which is a finger moving down when the
                        // content is meant to follow the finger.
                        val sign = if (naturalScroll) 1.0 else -1.0
                        listener?.onScroll(stepY * sign, -stepX * sign)
                    }
                    return true
                }

                val dx = event.x - lastX
                val dy = event.y - lastY
                lastX = event.x; lastY = event.y
                if (!moved && hypot(event.x - downX, event.y - downY) > touchSlop) {
                    moved = true
                    removeCallbacks(longPress)
                    // A drag that began where a tap just ended is a press-and-drag, which is how
                    // a window gets moved or text selected.
                    if (System.currentTimeMillis() - downAt < tapTimeout &&
                        hypot(downX - lastX, downY - lastY) < holdSlop
                    ) {
                        // left as a plain move; the double-tap-drag case is handled by the host
                    }
                }
                if (moved) send(dx, dy)
            }

            MotionEvent.ACTION_POINTER_UP -> {
                if (event.pointerCount <= 2) {
                    scrolling = false
                    // Carry on from the remaining finger rather than jumping.
                    val remaining = if (event.actionIndex == 0) 1 else 0
                    lastX = event.getX(remaining); lastY = event.getY(remaining)
                    moved = true
                }
            }

            MotionEvent.ACTION_UP -> {
                removeCallbacks(longPress)
                if (dragging) {
                    listener?.onButton("left", false)
                    dragging = false
                } else if (!moved && !scrolling && System.currentTimeMillis() - downAt < longPressTimeout) {
                    listener?.onButton("left", true)
                    listener?.onButton("left", false)
                }
                scrolling = false
            }

            MotionEvent.ACTION_CANCEL -> {
                removeCallbacks(longPress)
                if (dragging) {
                    listener?.onButton("left", false)
                    dragging = false
                }
                scrolling = false
            }
        }
        return true
    }

    /** Holds the left button down until the next lift: the host calls this for a drag gesture. */
    fun beginDrag() {
        if (!dragging) {
            dragging = true
            listener?.onButton("left", true)
        }
    }

    private fun send(dx: Float, dy: Float) {
        val exactX = dx * scale + remainderX
        val exactY = dy * scale + remainderY
        val wholeX = exactX.toInt()
        val wholeY = exactY.toInt()
        // Fractions stay here, as the wire asks; only whole pixels go out.
        remainderX = exactX - wholeX
        remainderY = exactY - wholeY
        if (wholeX != 0 || wholeY != 0) listener?.onMove(wholeX, wholeY)
    }

    private fun midX(event: MotionEvent) = (event.getX(0) + event.getX(1)) / 2f
    private fun midY(event: MotionEvent) = (event.getY(0) + event.getY(1)) / 2f
}
