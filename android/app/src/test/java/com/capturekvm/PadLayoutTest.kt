package com.capturekvm

import com.capturekvm.ui.PadLayout
import com.capturekvm.ui.PadLayout.Control
import com.capturekvm.wire.PadWire
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The on-screen pad's geometry and its arithmetic.
 *
 * None of this is visible when it is wrong in the ways that matter. An inverted vertical axis
 * looks exactly like a correct one until you push up and the game looks down; a stick that
 * reaches 0.9 of full feels like a slow character rather than a bug; a dead zone that is too
 * wide eats a careful lean and is blamed on the link. The drawing can be checked by looking at
 * it, so [PadLayout] holds everything that cannot be, and this file is why it is a separate
 * class with no Android type in it.
 *
 * Two shapes are used throughout: a phone held in landscape, and a tablet.
 */
class PadLayoutTest {

    private val phone = PadLayout(width = 2340f, height = 1080f, density = 3f)      // ~780x360dp
    private val tablet = PadLayout(width = 2560f, height = 1600f, density = 2f)     // 1280x800dp
    private val small = PadLayout(width = 1280f, height = 600f, density = 2f)      // 640x300dp

    // --- the sticks ---

    @Test
    fun `a stick at rest reads zero`() {
        val middle = phone.leftStick
        assertEquals(0 to 0, phone.stick(Control.LEFT_STICK, middle.cx, middle.cy))
    }

    @Test
    fun `a thumb resting just off centre is still zero`() {
        // The dead zone. Without one, a thumb that settles a few pixels off centre walks the
        // game's camera on its own.
        val middle = phone.leftStick
        val nudge = middle.radius * 0.05f
        assertEquals(0 to 0, phone.stick(Control.LEFT_STICK, middle.cx + nudge, middle.cy))
    }

    @Test
    fun `pushing right reaches full right`() {
        val middle = phone.leftStick
        val (x, y) = phone.stick(Control.LEFT_STICK, middle.cx + middle.radius, middle.cy)
        assertEquals(PadWire.AXIS_MAX, x)
        assertEquals(0, y)
    }

    @Test
    fun `pushing up is positive, because XUSB counts up and Android counts down`() {
        val middle = phone.rightStick
        val (x, y) = phone.stick(Control.RIGHT_STICK, middle.cx, middle.cy - middle.radius)
        assertEquals(0, x)
        assertEquals(PadWire.AXIS_MAX, y)
    }

    @Test
    fun `pushing down is negative`() {
        val middle = phone.rightStick
        val (_, y) = phone.stick(Control.RIGHT_STICK, middle.cx, middle.cy + middle.radius)
        assertTrue("expected a negative y, got $y", y < 0)
        assertEquals(PadWire.AXIS_MIN + 1, y)   // -32767: the axis is symmetric about zero
    }

    @Test
    fun `sliding a thumb past the edge holds full deflection`() {
        // Not clamped back to the circle's edge value -- held there. A thumb that drifts off a
        // stick mid-sprint should keep sprinting, not stop.
        val middle = phone.leftStick
        val far = phone.stick(Control.LEFT_STICK, middle.cx + middle.radius * 4, middle.cy)
        assertEquals(PadWire.AXIS_MAX to 0, far)
    }

    @Test
    fun `a diagonal never exceeds the axis range`() {
        val middle = phone.leftStick
        val reach = middle.radius * 3
        val (x, y) = phone.stick(Control.LEFT_STICK, middle.cx + reach, middle.cy - reach)
        assertTrue("$x out of range", x in PadWire.AXIS_MIN..PadWire.AXIS_MAX)
        assertTrue("$y out of range", y in PadWire.AXIS_MIN..PadWire.AXIS_MAX)
        // Clamped round, not square: a corner lean is the same magnitude as a straight one.
        val magnitude = Math.hypot(x.toDouble(), y.toDouble())
        assertEquals(PadWire.AXIS_MAX.toDouble(), magnitude, 64.0)
    }

    @Test
    fun `the knob stays inside its zone`() {
        val middle = tablet.rightStick
        val (kx, ky) = tablet.knob(Control.RIGHT_STICK, middle.cx + middle.radius * 5, middle.cy)
        val reach = Math.hypot((kx - middle.cx).toDouble(), (ky - middle.cy).toDouble())
        assertTrue("knob escaped at $reach", reach <= middle.radius)
    }

    // --- the D-pad ---

    @Test
    fun `the middle of the D-pad is no direction at all`() {
        assertEquals(0, phone.dpadMask(phone.dpad.cx, phone.dpad.cy))
    }

    @Test
    fun `each edge of the D-pad is its own direction`() {
        val d = phone.dpad
        assertEquals(PadWire.BTN_DPAD_UP, phone.dpadMask(d.cx, d.cy - d.radius))
        assertEquals(PadWire.BTN_DPAD_DOWN, phone.dpadMask(d.cx, d.cy + d.radius))
        assertEquals(PadWire.BTN_DPAD_LEFT, phone.dpadMask(d.cx - d.radius, d.cy))
        assertEquals(PadWire.BTN_DPAD_RIGHT, phone.dpadMask(d.cx + d.radius, d.cy))
    }

    @Test
    fun `a corner of the D-pad is a diagonal`() {
        val d = phone.dpad
        val corner = d.radius * 0.7f
        assertEquals(PadWire.BTN_DPAD_UP or PadWire.BTN_DPAD_RIGHT,
                     phone.dpadMask(d.cx + corner, d.cy - corner))
    }

    // --- hit testing ---

    @Test
    fun `every control can be hit at its own centre`() {
        for (layout in listOf(phone, tablet)) {
            for (control in Control.values()) {
                val found = when (control) {
                    Control.LEFT_STICK -> layout.hit(layout.leftStick.cx, layout.leftStick.cy)
                    Control.RIGHT_STICK -> layout.hit(layout.rightStick.cx, layout.rightStick.cy)
                    Control.DPAD -> layout.hit(layout.dpad.cx, layout.dpad.cy)
                    else -> {
                        val circle = layout.faces[control]
                        val pill = layout.pills[control]
                        assertTrue("$control has no place on the pad", circle != null || pill != null)
                        if (circle != null) layout.hit(circle.cx, circle.cy)
                        else layout.hit(pill!!.cx, pill.cy)
                    }
                }
                assertEquals("$control did not answer at its own centre", control, found)
            }
        }
    }

    @Test
    fun `the middle of the screen belongs to nothing`() {
        // Where the picture is. A touch there must not be read as a control, or a stray thumb
        // would press something.
        assertNull(phone.hit(phone.width / 2f, phone.height / 2f))
    }

    @Test
    fun `every control answers at its own centre`() {
        for (layout in listOf(phone, tablet, small)) {
            val spots = buildList {
                add(Control.LEFT_STICK to (layout.leftStick.cx to layout.leftStick.cy))
                add(Control.RIGHT_STICK to (layout.rightStick.cx to layout.rightStick.cy))
                add(Control.DPAD to (layout.dpad.cx to layout.dpad.cy))
                layout.faces.forEach { (c, circle) -> add(c to (circle.cx to circle.cy)) }
                layout.pills.forEach { (c, pill) -> add(c to (pill.cx to pill.cy)) }
            }
            for ((control, spot) in spots) {
                assertEquals("$control is covered by something else",
                             control, layout.hit(spot.first, spot.second))
            }
        }
    }

    @Test
    fun `no two controls are drawn over each other`() {
        // Not the same test as the one above, which only asks what sits at each centre -- and
        // passed while the A button was drawn across the top of the right stick, because A's
        // own centre still answered A. This one measures the shapes. It is the test the bug
        // actually needed, and it came from looking at the rendered layout, not from the code.
        for ((name, layout) in listOf("phone" to phone, "tablet" to tablet, "small" to small)) {
            val circles = buildList {
                add(Control.LEFT_STICK to layout.leftStick)
                add(Control.RIGHT_STICK to layout.rightStick)
                add(Control.DPAD to layout.dpad)
                layout.faces.forEach { (c, circle) -> add(c to circle) }
            }
            for (i in circles.indices) {
                for (j in i + 1 until circles.size) {
                    val (oneName, one) = circles[i]
                    val (twoName, two) = circles[j]
                    val apart = Math.hypot((one.cx - two.cx).toDouble(), (one.cy - two.cy).toDouble())
                    assertTrue(
                        "$name: $oneName and $twoName overlap by ${one.radius + two.radius - apart}px",
                        apart >= one.radius + two.radius)
                }
            }
            for ((circleName, circle) in circles) {
                for ((pillName, pill) in layout.pills) {
                    val nearestX = circle.cx.coerceIn(pill.left, pill.right)
                    val nearestY = circle.cy.coerceIn(pill.top, pill.bottom)
                    val apart = Math.hypot((circle.cx - nearestX).toDouble(),
                                           (circle.cy - nearestY).toDouble())
                    assertTrue("$name: $circleName overlaps $pillName", apart >= circle.radius)
                }
            }
            val pills = layout.pills.entries.toList()
            for (i in pills.indices) {
                for (j in i + 1 until pills.size) {
                    val one = pills[i].value
                    val two = pills[j].value
                    val overlaps = one.left < two.right && two.left < one.right &&
                        one.top < two.bottom && two.top < one.bottom
                    assertTrue("$name: ${pills[i].key} overlaps ${pills[j].key}", !overlaps)
                }
            }
        }
    }

    @Test
    fun `the corner buttons have the corners to themselves`() {
        // The stream's menu and keyboard buttons sit in the top corners, above this view. A pad
        // control under either would be unreachable there -- the button takes the touch first.
        for ((name, layout) in listOf("phone" to phone, "tablet" to tablet, "small" to small)) {
            val shapes = buildList {
                add(Control.LEFT_STICK to layout.leftStick)
                add(Control.RIGHT_STICK to layout.rightStick)
                add(Control.DPAD to layout.dpad)
                layout.faces.forEach { (c, circle) -> add(c to circle) }
            }
            for (corner in layout.corners) {
                for ((control, circle) in shapes) {
                    val nearestX = circle.cx.coerceIn(corner.left, corner.right)
                    val nearestY = circle.cy.coerceIn(corner.top, corner.bottom)
                    val apart = Math.hypot((circle.cx - nearestX).toDouble(),
                                           (circle.cy - nearestY).toDouble())
                    assertTrue("$name: $control reaches into a corner", apart >= circle.radius)
                }
                for ((control, pill) in layout.pills) {
                    val overlaps = pill.left < corner.right && corner.left < pill.right &&
                        pill.top < corner.bottom && corner.top < pill.bottom
                    assertTrue("$name: $control is under a corner button", !overlaps)
                }
            }
        }
    }

    @Test
    fun `everything is on the screen`() {
        for (layout in listOf(phone, tablet)) {
            val circles = listOf(layout.leftStick, layout.rightStick, layout.dpad) +
                layout.faces.values
            for (circle in circles) {
                assertTrue("a control hangs off the left", circle.cx - circle.radius >= 0f)
                assertTrue("a control hangs off the right", circle.cx + circle.radius <= layout.width)
                assertTrue("a control hangs off the top", circle.cy - circle.radius >= 0f)
                assertTrue("a control hangs off the bottom", circle.cy + circle.radius <= layout.height)
            }
            for (pill in layout.pills.values) {
                assertTrue("a pill hangs off an edge",
                           pill.left >= 0f && pill.right <= layout.width &&
                               pill.top >= 0f && pill.bottom <= layout.height)
            }
        }
    }

    // --- the mapping to the wire ---

    @Test
    fun `the face and shoulder buttons carry their XUSB bits`() {
        assertEquals(PadWire.BTN_A, PadLayout.buttonFor(Control.A))
        assertEquals(PadWire.BTN_B, PadLayout.buttonFor(Control.B))
        assertEquals(PadWire.BTN_X, PadLayout.buttonFor(Control.X))
        assertEquals(PadWire.BTN_Y, PadLayout.buttonFor(Control.Y))
        assertEquals(PadWire.BTN_LEFT_SHOULDER, PadLayout.buttonFor(Control.LB))
        assertEquals(PadWire.BTN_RIGHT_SHOULDER, PadLayout.buttonFor(Control.RB))
        assertEquals(PadWire.BTN_BACK, PadLayout.buttonFor(Control.BACK))
        assertEquals(PadWire.BTN_START, PadLayout.buttonFor(Control.START))
        assertEquals(PadWire.BTN_GUIDE, PadLayout.buttonFor(Control.GUIDE))
    }

    @Test
    fun `the triggers and the sticks are not buttons`() {
        for (control in listOf(Control.LT, Control.RT, Control.LEFT_STICK,
                               Control.RIGHT_STICK, Control.DPAD)) {
            assertNull("$control should be read as an axis", PadLayout.buttonFor(control))
        }
    }

    @Test
    fun `a report built from this pad survives the wire`() {
        // The whole point: what a thumb does here is what the PC's virtual pad is told.
        val middle = phone.leftStick
        val state = PadWire.State()
        val (lx, ly) = phone.stick(Control.LEFT_STICK, middle.cx, middle.cy - middle.radius)
        state.lx = lx
        state.ly = ly
        state.setButton(PadLayout.buttonFor(Control.A)!!, true)
        state.rt = PadWire.TRIGGER_MAX

        val data = PadWire.report(state, 0).getJSONObject("data")
        assertEquals(0, data.getInt("lx"))
        assertEquals(PadWire.AXIS_MAX, data.getInt("ly"))
        assertEquals(PadWire.BTN_A, data.getInt("buttons"))
        assertEquals(PadWire.TRIGGER_MAX, data.getInt("rt"))
        assertEquals(0, data.getInt("lt"))
    }

    @Test
    fun `a short screen still gets a whole pad`() {
        // A small phone in landscape is the tightest case; the layout shrinks rather than
        // spilling controls off the edges or on top of each other.
        assertNotNull(small.hit(small.leftStick.cx, small.leftStick.cy))
        assertTrue(small.dpad.cy - small.dpad.radius > small.pills[Control.LT]!!.bottom)
        assertTrue(small.leftStick.cy + small.leftStick.radius <= small.height)
        assertEquals(Control.DPAD, small.hit(small.dpad.cx, small.dpad.cy))
    }
}
