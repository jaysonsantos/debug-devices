package dev.jayson.debugdevices.camera

import java.io.File
import kotlin.math.abs
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.float
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The Kotlin layout against the shared vectors (`docs/overlay-layout-vectors.json`, made by the Python reference). */
class OverlayLayoutTest {
    private val vectors = File(VECTORS_PATH).readText().let { Json.parseToJsonElement(it).jsonObject }
    private val tolerance = vectors.getValue("tolerance").jsonPrimitive.float + FLOAT_SLACK

    @Test
    fun `every shared vector`() {
        val cases = vectors.getValue("cases").jsonArray
        assertTrue(cases.isNotEmpty())
        for (case in cases.map { it.jsonObject }) {
            val name = case.getValue("name").jsonPrimitive.content
            val result = OverlayLayout.layout(input(case.getValue("input").jsonObject))
            check(name, case.getValue("expected").jsonObject, result)
        }
    }

    private fun input(json: JsonObject) = LayoutInput(
        width = json.f("width"),
        height = json.f("height"),
        minBox = json.f("min_box"),
        boxes = json.getValue("boxes").jsonArray.map {
            val b = it.jsonObject
            LayoutBoxInput(
                b.f("x"),
                b.f("y"),
                b.f("width"),
                b.f("height"),
                b.s("tag"),
                b.getValue("label").jsonPrimitive.content
            )
        },
        arrows = json.getValue("arrows").jsonArray.map {
            val a = it.jsonObject
            LayoutArrowInput(a.f("angle_deg"), a.s("tag"), a.getValue("label").jsonPrimitive.content)
        },
        inset = json.getValue("inset").jsonPrimitive.boolean
    )

    private fun check(name: String, expected: JsonObject, result: LayoutResult) {
        val boxes = expected.getValue("boxes").jsonArray.map { it.jsonObject }
        assertEquals(name, boxes.size, result.boxes.size)
        boxes.forEachIndexed { i, b ->
            assertEquals("$name box $i tag", b.s("tag"), result.boxes[i].tag)
            assertEquals("$name box $i colour", b.s("colour"), result.boxes[i].colour)
            rect("$name box $i", b.getValue("rect"), result.boxes[i].rect)
        }
        val badges = expected.getValue("badges").jsonArray.map { it.jsonObject }
        assertEquals(name, badges.size, result.badges.size)
        badges.forEachIndexed { i, b ->
            val actual = result.badges[i]
            assertEquals("$name badge $i tag", b.s("tag"), actual.tag)
            rect("$name badge $i", b.getValue("rect"), actual.rect)
            assertEquals("$name badge $i outside", b.getValue("outside").jsonPrimitive.boolean, actual.outside)
            val leader = b.getValue("leader")
            if (leader is JsonNull) {
                assertNull("$name badge $i leader", actual.leader)
            } else {
                val v = leader.jsonArray.map { it.jsonPrimitive.float }
                val l = requireNotNull(actual.leader) { "$name badge $i leader" }
                near("$name badge $i leader", v, listOf(l.first.x, l.first.y, l.second.x, l.second.y))
            }
        }
        val legend = expected.getValue("legend")
        if (legend is JsonNull) {
            assertNull("$name legend", result.legend)
        } else {
            val l = requireNotNull(result.legend) { "$name legend" }
            rect("$name legend", legend.jsonObject.getValue("rect"), l.rect)
            assertEquals("$name legend outside", legend.jsonObject.getValue("outside").jsonPrimitive.boolean, l.outside)
            val rows = legend.jsonObject.getValue("rows").jsonArray.map { it.jsonObject }
            assertEquals(
                "$name legend rows",
                rows.map {
                    it.s("tag") to it.s("label")
                },
                l.rows.map { it.tag to it.label }
            )
            assertEquals("$name legend colours", rows.map { it.s("colour") }, l.rows.map { it.colour })
        }
        val arrows = expected.getValue("arrows").jsonArray.map { it.jsonObject }
        assertEquals(name, arrows.size, result.arrows.size)
        arrows.forEachIndexed { i, a ->
            assertEquals("$name arrow $i tag", a.s("tag"), result.arrows[i].tag)
            assertEquals("$name arrow $i colour", a.s("colour"), result.arrows[i].colour)
            val anchor = a.getValue("anchor").jsonArray.map { it.jsonPrimitive.float }
            near("$name arrow $i anchor", anchor, listOf(result.arrows[i].anchor.x, result.arrows[i].anchor.y))
        }
        val inset = expected["inset"]
        if (inset == null || inset is JsonNull) {
            assertNull("$name inset", result.inset)
        } else {
            val actual = requireNotNull(result.inset) { "$name inset" }
            rect("$name inset source", inset.jsonObject.getValue("source"), actual.source)
            rect("$name inset dest", inset.jsonObject.getValue("dest"), actual.dest)
            near("$name inset scale", listOf(inset.jsonObject.f("scale")), listOf(actual.scale))
        }
        near("$name extra height", listOf(expected.f("extra_height")), listOf(result.extraHeight))
    }

    private fun rect(label: String, json: JsonElement, actual: PixelRect) {
        val r = json.jsonObject
        near(
            label,
            listOf(r.f("x"), r.f("y"), r.f("width"), r.f("height")),
            listOf(
                actual.left,
                actual.top,
                actual.right - actual.left,
                actual.bottom - actual.top
            )
        )
    }

    private fun near(label: String, expected: List<Float>, actual: List<Float>) {
        assertEquals(label, expected.size, actual.size)
        expected.zip(actual).forEach { (e, a) ->
            assertTrue(
                "$label: expected $expected, got $actual",
                abs(e - a) <= tolerance
            )
        }
    }

    private fun JsonObject.f(key: String) = getValue(key).jsonPrimitive.float

    private fun JsonObject.s(key: String): String? = this[key]?.jsonPrimitive?.contentOrNull

    @Test
    fun `tags keep given ones (3 characters) and give the first free letter`() {
        assertEquals(listOf("A", "B", "C"), OverlayLayout.tags(listOf(null, null, null)))
        assertEquals(listOf("U1", "B", "A", "C"), OverlayLayout.tags(listOf("U1", null, "A", null)))
        assertEquals(listOf("U73"), OverlayLayout.tags(listOf("U7301")))
    }

    @Test
    fun `a hidden box keeps its tag, colour, and legend row (B-S2)`() {
        // Two boxes, the first outside the phone view: the second stays "B" in cyan, like on the page.
        val hidden = LayoutBoxInput(0f, 0f, 10f, 10f, null, "off screen", visible = false)
        val shown = LayoutBoxInput(200f, 400f, 30f, 30f, null, "on screen")
        val result = OverlayLayout.layout(
            LayoutInput(411f, 914f, 24f, listOf(hidden, shown), emptyList(), inset = false)
        )
        assertEquals(listOf("B"), result.boxes.map { it.tag })
        assertEquals(listOf("#00E5FF"), result.boxes.map { it.colour })
        assertEquals(listOf("B"), result.badges.map { it.tag })
        assertEquals(listOf("A" to "off screen", "B" to "on screen"), result.legend?.rows?.map { it.tag to it.label })
        // The placement ignores the hidden box: the same as a layout of the visible box alone (apart from tag and rows).
        val alone = OverlayLayout.layout(LayoutInput(411f, 914f, 24f, listOf(shown), emptyList(), inset = false))
        assertEquals(alone.boxes.single().rect, result.boxes.single().rect)
        assertEquals(alone.badges.single().rect.left, result.badges.single().rect.left)
    }

    @Test
    fun `an outside legend knows the first corner of the sorted list (B-S1)`() {
        val rows = listOf(LegendRow("A", "x", OverlayLayout.colour(0)))
        // Legend 44 x 30. Boxes on three corners, and one 2 units from the bottom-right legend: it overlaps only when
        // grown by 4, so every corner overlaps, and bottom-right has the largest distance.
        val boxes = listOf(
            PixelRect(10f, 10f, 20f, 20f),
            PixelRect(350f, 10f, 360f, 20f),
            PixelRect(10f, 370f, 20f, 380f),
            PixelRect(394f, 370f, 399f, 380f)
        )
        val legend = OverlayLayout.placeLegend(400f, 400f, boxes, rows)
        assertTrue(legend.outside)
        assertEquals(PixelRect(348f, 362f, 392f, 392f), legend.firstCorner)
        // Not outside: the first corner is the sorted first, the rect is the chosen free one.
        val free = OverlayLayout.placeLegend(400f, 400f, listOf(PixelRect(10f, 10f, 20f, 20f)), rows)
        assertEquals(free.rect, free.firstCorner)
    }

    @Test
    fun `an arrow keeps its given tag, others get free letters (C3)`() {
        val box = LayoutBoxInput(100f, 100f, 30f, 30f, null, "U1")
        val arrows = listOf(LayoutArrowInput(0f, "J4", "connector"), LayoutArrowInput(90f, null, "fuse"))
        val result = OverlayLayout.layout(LayoutInput(411f, 914f, 24f, listOf(box), arrows, inset = false))
        assertEquals(listOf("J4", "B"), result.arrows.map { it.tag })
        assertEquals(listOf("A", "J4", "B"), result.legend?.rows?.map { it.tag })
    }

    @Test
    fun `the phone view is the safe area without the status label`() {
        // Portrait 1080 x 2340: status bar 100, navigation bar 50, label band 60 (at the viewer's top).
        val insets = PixelRect(0f, 100f, 0f, 50f)
        assertEquals(PixelRect(0f, 160f, 1080f, 2290f), OverlayLayout.phoneFrame(1080f, 2340f, insets, 0, 60f))
        // Viewer turned 90: the safe screen rect (0, 100)-(1080, 2290) becomes (100, 0)-(2290, 1080) for the viewer,
        // and the label band is at the viewer's top.
        assertEquals(PixelRect(100f, 60f, 2290f, 1080f), OverlayLayout.phoneFrame(1080f, 2340f, insets, 90, 60f))
        assertEquals(PixelRect(0f, 110f, 1080f, 2240f), OverlayLayout.phoneFrame(1080f, 2340f, insets, 180, 60f))
        assertEquals(PixelRect(50f, 60f, 2240f, 1080f), OverlayLayout.phoneFrame(1080f, 2340f, insets, 270, 60f))
        // A band taller than the area leaves an empty frame, never an inverted one.
        val tiny = OverlayLayout.phoneFrame(100f, 100f, PixelRect(0f, 0f, 0f, 0f), 0, 500f)
        assertEquals(tiny.bottom, tiny.top)
    }

    @Test
    fun `the label band is measured only for the current orientation (N8, N10)`() {
        // Safe area 1080 x 2115 (portrait), label bottom 163, gap 12.
        assertEquals(175f, OverlayLayout.labelBand(1080, 1080, 2115, 0, 163, 12f))
        assertEquals(175f, OverlayLayout.labelBand(1080, 1080, 2115, 180, 163, 12f))
        // Sideways the turned container is as wide as the safe area is high.
        assertEquals(64f, OverlayLayout.labelBand(2115, 1080, 2115, 90, 52, 12f))
        // Right after a turn, the container still has the old width: not measured yet.
        assertNull(OverlayLayout.labelBand(1080, 1080, 2115, 90, 163, 12f))
        assertNull(OverlayLayout.labelBand(2115, 1080, 2115, 0, 52, 12f))
        // The safe area has no size yet.
        assertNull(OverlayLayout.labelBand(0, 0, 0, 0, 0, 12f))
        assertNull(OverlayLayout.labelBand(1080, 1080, 0, 0, 0, 12f))
    }

    @Test
    fun `from viewer is the inverse of to viewer`() {
        val r = PixelRect(100f, 200f, 150f, 260f)
        for (degrees in listOf(0, 90, 180, 270)) {
            val viewer = OverlayLayout.toViewer(r, degrees, 1080f, 2340f)
            assertEquals("$degrees", r, OverlayLayout.fromViewer(viewer, degrees, 1080f, 2340f))
        }
    }

    @Test
    fun `a box under the status bar is outside the phone view`() {
        val frame = PixelRect(0f, 160f, 1080f, 2290f)
        assertFalse(OverlayLayout.inFrame(PixelRect(100f, 20f, 200f, 90f), frame))
        assertFalse(OverlayLayout.inFrame(PixelRect(100f, 2295f, 200f, 2330f), frame))
        assertTrue(OverlayLayout.inFrame(PixelRect(100f, 150f, 200f, 200f), frame))
        // Touching the edge does not count.
        assertFalse(OverlayLayout.inFrame(PixelRect(100f, 100f, 200f, 160f), frame))
    }

    @Test
    fun `the phone has no inset`() {
        val tiny = LayoutBoxInput(200f, 400f, 4f, 4f, null, "tiny")
        assertNull(OverlayLayout.layout(LayoutInput(411f, 914f, 24f, listOf(tiny), emptyList(), inset = false)).inset)
    }

    @Test
    fun `argb of a spec colour`() {
        assertEquals(0xFF00E676.toInt(), OverlayLayout.argb("#00E676"))
    }

    @Test
    fun `screen to viewer frame for each turn`() {
        val r = PixelRect(100f, 200f, 150f, 210f)
        assertEquals(r, OverlayLayout.toViewer(r, 0, 1000f, 2000f))
        assertEquals(PixelRect(200f, 850f, 210f, 900f), OverlayLayout.toViewer(r, 90, 1000f, 2000f))
        assertEquals(PixelRect(850f, 1790f, 900f, 1800f), OverlayLayout.toViewer(r, 180, 1000f, 2000f))
        assertEquals(PixelRect(1790f, 100f, 1800f, 150f), OverlayLayout.toViewer(r, 270, 1000f, 2000f))
    }

    private companion object {
        /** Gradle runs unit tests in the module directory (`android/app`). */
        const val VECTORS_PATH = "../../docs/overlay-layout-vectors.json"

        /** Float rounding on top of the vector tolerance. */
        const val FLOAT_SLACK = 1e-3f
    }
}
