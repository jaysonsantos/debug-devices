package dev.jayson.debugdevices.camera

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.floatOrNull

// kotlinx.serialization reads "2" into a Float and "true" into a Boolean. The contract wants JSON numbers and
// booleans only, so these serializers refuse a JSON string.

object StrictFloatSerializer : KSerializer<Float> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor("StrictFloat", PrimitiveKind.FLOAT)

    override fun deserialize(decoder: Decoder): Float =
        decodeLiteral(decoder).floatOrNull ?: throw SerializationException(Constants.Messages.EXPECTED_NUMBER)

    override fun serialize(encoder: Encoder, value: Float) = encoder.encodeFloat(value)
}

object StrictBooleanSerializer : KSerializer<Boolean> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor("StrictBoolean", PrimitiveKind.BOOLEAN)

    override fun deserialize(decoder: Decoder): Boolean =
        decodeLiteral(decoder).booleanOrNull ?: throw SerializationException(Constants.Messages.EXPECTED_BOOLEAN)

    override fun serialize(encoder: Encoder, value: Boolean) = encoder.encodeBoolean(value)
}

/** Returns the JSON value when it is a literal (number, boolean), not a string, array, or object. */
private fun decodeLiteral(decoder: Decoder): JsonPrimitive {
    val jsonDecoder = decoder as? JsonDecoder ?: throw SerializationException(Constants.Messages.JSON_ONLY)
    val element = jsonDecoder.decodeJsonElement()
    if (element !is JsonPrimitive || element.isString) {
        throw SerializationException(Constants.Messages.EXPECTED_LITERAL)
    }
    return element
}
