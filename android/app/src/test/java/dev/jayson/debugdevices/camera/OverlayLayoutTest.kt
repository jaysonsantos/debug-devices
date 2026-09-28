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
