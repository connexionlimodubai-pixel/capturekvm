package com.capturekvm

import com.capturekvm.wire.Crypto
import com.capturekvm.wire.Pairing
import com.capturekvm.wire.Wire
import java.math.BigInteger
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Kotlin wire and pairing code against values Beamer's own Python produced.
 *
 * This is the test that matters. The Android app speaks the protocol by transcription, not by
 * sharing code, and one wrong byte anywhere in a derivation means pairing silently fails or the
 * link closes on a tag failure -- symptoms that say nothing about the cause. Regenerate the
 * vectors with `python tools/make_vectors.py` after touching the vendored Beamer core.
 */
class WireVectorsTest {

    private val vectors: JSONObject by lazy {
        val stream = javaClass.classLoader!!.getResourceAsStream("wire_vectors.json")
            ?: error("wire_vectors.json is missing; run python tools/make_vectors.py")
        JSONObject(stream.bufferedReader().readText())
    }

    private fun hex(text: String): ByteArray {
        if (text.isEmpty()) return ByteArray(0)
        return ByteArray(text.length / 2) { index ->
            text.substring(index * 2, index * 2 + 2).toInt(16).toByte()
        }
    }

    private fun hexOf(data: ByteArray): String =
        data.joinToString("") { "%02x".format(it) }

    @Test
    fun `link version matches`() {
        assertEquals(vectors.getInt("link_version"), Wire.VERSION)
    }

    @Test
    fun `b64 writes and reads the one spelling`() {
        val cases = vectors.getJSONArray("b64")
        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            val bytes = hex(case.getString("bytes"))
            val text = case.getString("text")
            assertEquals("encoding ${case.getString("bytes")}", text, Wire.b64(bytes))
            if (text.isNotEmpty()) {
                assertArrayEquals("decoding $text", bytes, Wire.unb64(text))
            }
        }
    }

    @Test
    fun `b64 refuses a non-canonical spelling`() {
        // "AAECAwQFBgcICQoLDA0ODx" is the canonical form of those 16 bytes; a trailing bit set in
        // the final character decodes to the same bytes and must be refused.
        var threw = false
        try {
            Wire.unb64("AAECAwQFBgcICQoLDA0ODz")
        } catch (exc: Wire.WireError) {
            threw = true
        }
        assertTrue("a non-canonical spelling must be refused", threw)
    }

    @Test
    fun `hkdf matches`() {
        val case = vectors.getJSONObject("hkdf")
        assertEquals(
            case.getString("out"),
            hexOf(Crypto.hkdfSha256(hex(case.getString("ikm")), hex(case.getString("salt")), hex(case.getString("info")))),
        )
    }

    @Test
    fun `key id matches`() {
        val case = vectors.getJSONObject("key_id")
        assertEquals(case.getString("out"), hexOf(Wire.keyId(case.getString("token"))))
    }

    @Test
    fun `x25519 matches`() {
        val case = vectors.getJSONObject("x25519")
        assertEquals(case.getString("share_i"), hexOf(Crypto.x25519Base(hex(case.getString("secret_i")))))
        assertEquals(case.getString("share_r"), hexOf(Crypto.x25519Base(hex(case.getString("secret_r")))))
        assertEquals(
            case.getString("shared"),
            hexOf(Crypto.x25519(hex(case.getString("secret_i")), hex(case.getString("share_r")))),
        )
        assertEquals(
            "both sides agree",
            case.getString("shared"),
            hexOf(Crypto.x25519(hex(case.getString("secret_r")), hex(case.getString("share_i")))),
        )
    }

    @Test
    fun `link info and both direction keys match`() {
        val keys = vectors.getJSONObject("link_keys")
        val x = vectors.getJSONObject("x25519")
        val token = keys.getString("token")
        val keyId = Wire.keyId(token)
        val prefixI = hex(keys.getString("prefix_i"))
        val prefixR = hex(keys.getString("prefix_r"))
        val shareI = hex(x.getString("share_i"))
        val shareR = hex(x.getString("share_r"))
        val shared = hex(x.getString("shared"))

        assertEquals(
            vectors.getJSONObject("link_info").getString("out"),
            hexOf(Wire.linkInfo(keyId, prefixI, prefixR, shareI, shareR, Wire.DIRECTION_INITIATOR)),
        )
        assertEquals(
            keys.getString("initiator"),
            hexOf(Wire.linkKey(token, shared, keyId, prefixI, prefixR, shareI, shareR, Wire.DIRECTION_INITIATOR)),
        )
        assertEquals(
            keys.getString("responder"),
            hexOf(Wire.linkKey(token, shared, keyId, prefixI, prefixR, shareI, shareR, Wire.DIRECTION_RESPONDER)),
        )
    }

    @Test
    fun `handshake produces the same preamble and the same sealed frames`() {
        val case = vectors.getJSONObject("handshake")
        val session = Wire.Session(
            case.getString("token"),
            hex(case.getString("prefix_i")),
            hex(case.getString("secret_i")),
        )
        assertEquals(case.getString("initiator_preamble"), hexOf(session.preamble()))

        session.acceptPreamble(hex(case.getString("responder_preamble")))

        val frames = case.getJSONArray("initiator_frames")
        for (index in 0 until frames.length()) {
            val frame = frames.getJSONObject(index)
            val sealed = session.sealRaw(frame.getString("plain").toByteArray(Charsets.UTF_8))
            assertEquals("frame $index", frame.getString("frame"), hexOf(sealed))
        }
    }

    @Test
    fun `a frame from the responder opens`() {
        val case = vectors.getJSONObject("handshake")
        val session = Wire.Session(
            case.getString("token"),
            hex(case.getString("prefix_i")),
            hex(case.getString("secret_i")),
        )
        session.acceptPreamble(hex(case.getString("responder_preamble")))

        val whole = hex(case.getJSONObject("responder_frame").getString("frame"))
        // The length prefix is stripped by readFrame on the wire; open takes what follows.
        val body = whole.copyOfRange(Wire.HEADER_SIZE, whole.size)
        val plain = session.openRaw(body)
        assertEquals(
            case.getJSONObject("responder_frame").getString("plain"),
            String(plain, Charsets.UTF_8),
        )
    }

    @Test
    fun `a replayed counter is refused`() {
        val case = vectors.getJSONObject("handshake")
        val session = Wire.Session(
            case.getString("token"),
            hex(case.getString("prefix_i")),
            hex(case.getString("secret_i")),
        )
        session.acceptPreamble(hex(case.getString("responder_preamble")))
        val whole = hex(case.getJSONObject("responder_frame").getString("frame"))
        val body = whole.copyOfRange(Wire.HEADER_SIZE, whole.size)
        session.openRaw(body)
        var threw = false
        try {
            session.openRaw(body)
        } catch (exc: Wire.WireError) {
            threw = true
        }
        assertTrue("the same counter must not open twice", threw)
    }

    @Test
    fun `prepend_len and lv_cat match`() {
        val cases = vectors.getJSONArray("prepend_len")
        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            assertEquals(case.getString("out"), hexOf(Pairing.prependLen(hex(case.getString("bytes")))))
        }
        val lv = vectors.getJSONObject("lv_cat")
        val parts = lv.getJSONArray("parts")
        val arrays = (0 until parts.length()).map { hex(parts.getString(it)) }.toTypedArray()
        assertEquals(lv.getString("out"), hexOf(Pairing.lvCat(*arrays)))
    }

    @Test
    fun `elligator2 matches`() {
        val cases = vectors.getJSONArray("elligator2")
        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            assertEquals(
                "r=${case.getString("r")}",
                case.getString("out"),
                Crypto.elligator2(BigInteger(case.getString("r"))).toString(),
            )
        }
    }

    @Test
    fun `the whole CPace exchange agrees on the token and every tag`() {
        val case = vectors.getJSONObject("cpace")
        val sid = Pairing.sessionId(case.getString("pair_id"), hex(case.getString("nonce")))
        assertEquals("session id", case.getString("sid"), hexOf(sid))

        assertEquals(
            "generator",
            case.getString("generator"),
            hexOf(Pairing.generator(case.getString("code").toByteArray(Charsets.UTF_8), CHANNEL_V3, sid)),
        )

        val shareA = Pairing.share(case.getString("code"), sid, hex(case.getString("scalar_a")))
        val shareB = Pairing.share(case.getString("code"), sid, hex(case.getString("scalar_b")))
        assertEquals("share a", case.getString("share_a"), hexOf(shareA))
        assertEquals("share b", case.getString("share_b"), hexOf(shareB))

        val shared = Crypto.x25519(hex(case.getString("scalar_a")), shareB)
        assertEquals("shared", case.getString("shared"), hexOf(shared))

        val requester = case.getJSONObject("requester")
        val host = case.getJSONObject("host")
        val ada = Pairing.party(
            "requester".toByteArray(Charsets.US_ASCII), hex(requester.getString("id")),
            requester.getString("name"), requester.getString("platform"), requester.getInt("port"),
        )
        val adb = Pairing.party(
            "host".toByteArray(Charsets.US_ASCII), hex(host.getString("id")),
            host.getString("name"), host.getString("platform"), host.getInt("port"),
        )
        assertEquals("requester AD", requester.getString("ad"), hexOf(ada))
        assertEquals("host AD", host.getString("ad"), hexOf(adb))

        val keys = Pairing.keys(sid, shared, shareA, ada, shareB, adb)
        assertEquals("token", case.getString("token"), keys.token)
        assertEquals("requester tag", case.getString("requester_tag"), hexOf(keys.requesterTag))
        assertEquals("host tag", case.getString("host_tag"), hexOf(keys.hostTag))
        assertEquals("done tag", case.getString("done_tag"), hexOf(keys.doneTag))

        // And the token is a shape the link will accept as a pairing token.
        assertTrue("the agreed token must be linkable", Wire.isPairedToken(keys.token))
    }

    private companion object {
        val CHANNEL_V3 = "beamer-pair-v3".toByteArray(Charsets.US_ASCII)
    }
}
