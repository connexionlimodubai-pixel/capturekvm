package com.capturekvm

import com.capturekvm.ui.PadLayout
import com.capturekvm.ui.PadLayout.Control
import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Draws the on-screen pad, from the same class the view draws from.
 *
 * [PadLayoutTest] checks numbers. None of those numbers would notice a pad that is arithmetically
 * perfect and looks wrong: a D-pad crowding the left stick, a shoulder button over the status
 * bar, a tablet laid out like a stretched phone. Looking at it is the only way to catch that, and
 * there is no Android device here to look at it on -- so the layout draws itself into an SVG under
 * `app/build/pad-layout/` that can be opened in anything.
 *
 * It is a test rather than a script so it cannot drift: it is compiled against the real
 * [PadLayout], runs with every other test, and fails if a control stops being drawn at all.
 */
class PadLayoutPreviewTest {

    private val shapes = mapOf(
        // A phone held in landscape, a small one, and a tablet.
        "phone" to PadLayout(2340f, 1080f, 3f),
        "small-phone" to PadLayout(1280f, 600f, 2f),
        "tablet" to PadLayout(2560f, 1600f, 2f),
    )

    @Test
    fun `every shape of screen draws a whole pad`() {
        val folder = File("build/pad-layout")
        folder.mkdirs()
        for ((name, layout) in shapes) {
            val file = File(folder, "$name.svg")
            file.writeText(draw(layout))
            val drawn = file.readText()
            for (control in Control.values()) {
                assertTrue("$control is missing from $name.svg", drawn.contains(tag(control)))
            }
            println("pad layout: " + file.absolutePath)
        }
    }

    private fun tag(control: Control) = "<!--" + control.name + "-->"

    private fun draw(layout: PadLayout): String {
        val out = StringBuilder()
        val w = layout.width.toInt()
        val h = layout.height.toInt()
        out.append("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"")
            .append(w).append("\" height=\"").append(h)
            .append("\" viewBox=\"0 0 ").append(w).append(" ").append(h).append("\">\n")
        out.append("<rect width=\"100%\" height=\"100%\" fill=\"#101619\"/>\n")
        out.append("<text x=\"").append(w / 2).append("\" y=\"").append(h / 2)
            .append("\" fill=\"#2A3A3F\" font-family=\"sans-serif\" font-size=\"48\"")
            .append(" text-anchor=\"middle\">the picture</text>\n")

        for (corner in layout.corners) {
            // Where the stream's menu and keyboard buttons sit, drawn as they will look.
            val r = 20f * (layout.cornerSize / PadLayout.CORNER_DP)
            val cx = (corner.left + corner.right) / 2f
            out.append("<circle cx=\"").append(cx).append("\" cy=\"").append(r * 1.6f)
                .append("\" r=\"").append(r).append("\" fill=\"#000\" fill-opacity=\"0.55\"")
                .append(" stroke=\"#fff\" stroke-opacity=\"0.3\"/>\n")
        }
        circle(out, layout.leftStick.cx, layout.leftStick.cy, layout.leftStick.radius, "", Control.LEFT_STICK)
        knob(out, layout.leftStick.cx, layout.leftStick.cy, layout.knobRadius)
        circle(out, layout.rightStick.cx, layout.rightStick.cy, layout.rightStick.radius, "", Control.RIGHT_STICK)
        knob(out, layout.rightStick.cx, layout.rightStick.cy, layout.knobRadius)
        circle(out, layout.dpad.cx, layout.dpad.cy, layout.dpad.radius, "+", Control.DPAD)

        for ((control, face) in layout.faces) {
            circle(out, face.cx, face.cy, face.radius, control.name, control)
        }
        for ((control, pill) in layout.pills) {
            val round = (pill.bottom - pill.top) / 2f
            out.append("<rect x=\"").append(pill.left).append("\" y=\"").append(pill.top)
                .append("\" width=\"").append(pill.right - pill.left)
                .append("\" height=\"").append(pill.bottom - pill.top)
                .append("\" rx=\"").append(round)
                .append("\" fill=\"#5BD1C7\" fill-opacity=\"0.14\" stroke=\"#5BD1C7\"")
                .append(" stroke-width=\"3\"/>\n")
            text(out, pill.cx, pill.cy, control.name, 22f)
            out.append(tag(control)).append("\n")
        }
        out.append("</svg>\n")
        return out.toString()
    }

    private fun circle(out: StringBuilder, cx: Float, cy: Float, r: Float,
                       label: String, control: Control) {
        out.append("<circle cx=\"").append(cx).append("\" cy=\"").append(cy)
            .append("\" r=\"").append(r)
            .append("\" fill=\"#5BD1C7\" fill-opacity=\"0.14\" stroke=\"#5BD1C7\"")
            .append(" stroke-width=\"3\"/>\n")
        if (label.isNotEmpty()) text(out, cx, cy, label, r * 0.8f)
        out.append(tag(control)).append("\n")
    }

    private fun knob(out: StringBuilder, cx: Float, cy: Float, r: Float) {
        out.append("<circle cx=\"").append(cx).append("\" cy=\"").append(cy)
            .append("\" r=\"").append(r).append("\" fill=\"#5BD1C7\" fill-opacity=\"0.55\"/>\n")
    }

    private fun text(out: StringBuilder, cx: Float, cy: Float, label: String, size: Float) {
        out.append("<text x=\"").append(cx).append("\" y=\"").append(cy + size * 0.35f)
            .append("\" fill=\"#E8F3F2\" font-family=\"sans-serif\" font-size=\"").append(size)
            .append("\" text-anchor=\"middle\">").append(label).append("</text>\n")
    }
}
