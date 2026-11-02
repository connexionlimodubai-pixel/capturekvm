package com.capturekvm.wire

import java.io.DataInputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import org.json.JSONArray
import org.json.JSONObject

/**
 * Beamer wire version 6: the preamble, the keys, the frames and the messages this app sends.
 *
 * Every constant and every derivation here is a transcription of `core/protocol.py` at the commit
 * recorded in `third_party/beamer/BEAMER_COMMIT`, checked by `WireVectorsTest` against values the
 * Python produced. Only the initiator's half exists: this app drives a PC and is never driven.
 */
object Wire {

    val MAGIC = "BEAMY".toByteArray(Charsets.US_ASCII)
    const val VERSION = 6
    const val DEFAULT_PORT = 24820
    const val PAIRING_PORT = 24821

    val LINK_SALT = "beamer-link-v6".toByteArray(Charsets.US_ASCII)
    val KEY_ID_INFO = "beamer-key-id".toByteArray(Charsets.US_ASCII)
    val KEY_INFO = "beamer-frames".toByteArray(Charsets.US_ASCII)

    const val HEADER_SIZE = 4
    const val COUNTER_SIZE = 4
    const val NONCE_PREFIX_SIZE = 8
    const val KEY_ID_SIZE = 16
    const val SHARE_SIZE = 32
    const val MACHINE_ID_SIZE = 16
    const val TOKEN_SIZE = 32

    const val PREAMBLE_HEAD_SIZE = 6
    const val PREAMBLE_SIZE = PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE + KEY_ID_SIZE + SHARE_SIZE
    const val MIN_FRAME_BYTES = COUNTER_SIZE + 1 + Crypto.TAG_SIZE
    const val MAX_FRAME_BYTES = 16 * 1024 * 1024
    const val HELLO_MAX_BYTES = 4096

    const val DIRECTION_INITIATOR = 1
    const val DIRECTION_RESPONDER = 2

    const val MSG_HELLO = "hello"
    const val MSG_WELCOME = "welcome"
    const val MSG_PING = "ping"
    const val MSG_ACK = "ack"
    const val MSG_FOCUS = "focus"
    const val MSG_ACCEPT = "accept"
    const val MSG_REFUSE = "refuse"
    const val MSG_ACCEPTS = "accepts"
    const val MSG_SWITCH = "switch"
    const val MSG_KEYDOWN = "keydown"
    const val MSG_KEYUP = "keyup"
    const val MSG_MOUSEMOVE = "mousemove"
    const val MSG_MOUSEDOWN = "mousedown"
    const val MSG_MOUSEUP = "mouseup"
    const val MSG_SCROLL = "scroll"
    const val MSG_TEXT = "text"
    const val MSG_CLIPBOARD = "clipboard"

    val INPUT_TYPES = setOf(MSG_KEYDOWN, MSG_KEYUP, MSG_MOUSEMOVE, MSG_MOUSEDOWN, MSG_MOUSEUP, MSG_SCROLL, MSG_TEXT)

    const val MOVE_MAX = 65535
    const val TEXT_MAX_BYTES = 4096
    const val CLIPBOARD_MAX_BYTES = 256 * 1024
    const val MAX_NAME_CHARS = 48

    class WireError(message: String) : Exception(message)
    class VersionMismatch(val peer: Int?) : Exception("the peer speaks link version $peer")

    // --- PAIRING.md's b64: url-safe, padding stripped, one spelling only ---
    //
    // Written out rather than taken from android.util.Base64 or java.util.Base64: the former is a
    // stub under JVM unit tests, which would leave WireVectorsTest checking nothing, and the
    // latter's url-safe decoder is strict about padding in a way this format is not.

    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    fun b64(data: ByteArray): String {
        val out = StringBuilder((data.size + 2) / 3 * 4)
        var index = 0
        while (index < data.size) {
            val remaining = data.size - index
            val first = data[index].toInt() and 0xFF
            val second = if (remaining > 1) data[index + 1].toInt() and 0xFF else 0
            val third = if (remaining > 2) data[index + 2].toInt() and 0xFF else 0
            val block = (first shl 16) or (second shl 8) or third
            out.append(ALPHABET[(block shr 18) and 0x3F])
            out.append(ALPHABET[(block shr 12) and 0x3F])
            if (remaining > 1) out.append(ALPHABET[(block shr 6) and 0x3F])
            if (remaining > 2) out.append(ALPHABET[block and 0x3F])
            index += 3
        }
        return out.toString()
    }

    fun unb64(text: String): ByteArray {
        // Padding is accepted on input, as the Python's decoder is handed it, but the canonical
        // check below rejects anything whose one spelling differs from what b64 writes.
        val body = text.trimEnd('=')
        if (body.any { ALPHABET.indexOf(it) < 0 }) throw WireError("bad base64")
        if (body.length % 4 == 1) throw WireError("bad base64")
        val out = ByteArray(body.length * 3 / 4)
        var written = 0
        var index = 0
        while (index < body.length) {
            val remaining = body.length - index
            var block = 0
            val taken = minOf(4, remaining)
            for (step in 0 until 4) {
                block = block shl 6
                if (step < taken) block = block or ALPHABET.indexOf(body[index + step])
            }
            if (taken > 1) out[written++] = ((block shr 16) and 0xFF).toByte()
            if (taken > 2) out[written++] = ((block shr 8) and 0xFF).toByte()
            if (taken > 3) out[written++] = (block and 0xFF).toByte()
            index += 4
        }
        val data = if (written == out.size) out else out.copyOf(written)
        if (b64(data) != body) throw WireError("bad base64")
        return data
    }

    fun readId(text: String?): ByteArray? {
        if (text == null) return null
        val data = try { unb64(text) } catch (exc: WireError) { return null }
        if (data.size != MACHINE_ID_SIZE || data.all { it == 0.toByte() }) return null
        return data
    }

    fun isPairedToken(token: String?): Boolean {
        if (token == null) return false
        val data = try { unb64(token) } catch (exc: WireError) { return false }
        return data.size == TOKEN_SIZE
    }

    // --- keys ---

    fun keyId(token: String): ByteArray {
        if (!isPairedToken(token)) throw WireError("a typed token has no key id")
        return Crypto.hkdfSha256(token.toByteArray(Charsets.US_ASCII), LINK_SALT, KEY_ID_INFO)
            .copyOf(KEY_ID_SIZE)
    }

    fun linkIkm(token: String, shared: ByteArray): ByteArray =
        token.toByteArray(Charsets.US_ASCII) + shared

    fun linkInfo(
        keyId: ByteArray, prefixI: ByteArray, prefixR: ByteArray,
        shareI: ByteArray, shareR: ByteArray, direction: Int,
    ): ByteArray {
        require(keyId.size == KEY_ID_SIZE) { "key id is 16 bytes" }
        require(prefixI.size == NONCE_PREFIX_SIZE && prefixR.size == NONCE_PREFIX_SIZE) { "a nonce prefix is 8 bytes" }
        require(shareI.size == SHARE_SIZE && shareR.size == SHARE_SIZE) { "a share is 32 bytes" }
        require(direction == DIRECTION_INITIATOR || direction == DIRECTION_RESPONDER) { "direction is 1 or 2" }
        return KEY_INFO + byteArrayOf(VERSION.toByte()) + keyId + prefixI + prefixR + shareI + shareR +
            byteArrayOf(direction.toByte())
    }

    fun linkKey(
        token: String, shared: ByteArray, keyId: ByteArray,
        prefixI: ByteArray, prefixR: ByteArray, shareI: ByteArray, shareR: ByteArray, direction: Int,
    ): ByteArray = Crypto.hkdfSha256(
        linkIkm(token, shared), LINK_SALT,
        linkInfo(keyId, prefixI, prefixR, shareI, shareR, direction),
    )

    // --- the session ---

    /**
     * One link's handshake and cipher state for the initiator. Built fresh per connection: it
     * draws a nonce prefix and an ephemeral secret, and both counters start at zero.
     */
    class Session(private val token: String, prefix: ByteArray? = null, secret: ByteArray? = null) {
        val prefix: ByteArray = prefix ?: Crypto.randomBytes(NONCE_PREFIX_SIZE)
        private var secret: ByteArray? = secret ?: Crypto.randomBytes(SHARE_SIZE)
        val keyId: ByteArray = keyId(token)
        val share: ByteArray = Crypto.x25519Base(this.secret!!)

        var peerPrefix: ByteArray? = null; private set
        var peerShare: ByteArray? = null; private set
        private var sendKey: ByteArray? = null
        private var recvKey: ByteArray? = null
        private var sendCounter = 0L
        private var recvCounter = 0L
        private val sendLock = Any()

        fun preamble(): ByteArray = MAGIC + byteArrayOf(VERSION.toByte()) + prefix + keyId + share

        fun acceptPreamble(data: ByteArray) {
            if (peerPrefix != null) throw WireError("second preamble on one connection")
            val mine = secret ?: throw WireError("this session refused a preamble; build a new one")
            if (data.size != PREAMBLE_SIZE) throw WireError("not a version 6 preamble")
            if (!data.copyOfRange(0, 5).contentEquals(MAGIC) || data[5].toInt() != VERSION) {
                throw WireError("not a version 6 preamble")
            }
            val theirPrefix = data.copyOfRange(PREAMBLE_HEAD_SIZE, PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE)
            val theirKeyId = data.copyOfRange(PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE, PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE + KEY_ID_SIZE)
            val theirShare = data.copyOfRange(PREAMBLE_SIZE - SHARE_SIZE, PREAMBLE_SIZE)
            if (!Crypto.constantTimeEquals(theirKeyId, keyId)) {
                throw WireError("the PC's preamble names another pair's key id")
            }
            if (theirPrefix.contentEquals(prefix)) {
                throw WireError("the PC sent this side's own nonce prefix back")
            }
            val shared = try {
                Crypto.x25519(mine, theirShare)
            } finally {
                secret = null
            }
            // The initiator's own prefix and share come first, as link_info's role order requires.
            sendKey = linkKey(token, shared, keyId, prefix, theirPrefix, share, theirShare, DIRECTION_INITIATOR)
            recvKey = linkKey(token, shared, keyId, prefix, theirPrefix, share, theirShare, DIRECTION_RESPONDER)
            peerPrefix = theirPrefix
            peerShare = theirShare
        }

        fun sealRaw(body: ByteArray): ByteArray = synchronized(sendLock) {
            val key = sendKey ?: throw WireError("nothing is sealed before the peer's preamble")
            if (sendCounter >= 0xFFFFFFFFL) throw WireError("frame counter exhausted; reconnect")
            sendCounter += 1
            val counter = beInt(sendCounter)
            val ciphertext = Crypto.seal(key, prefix + counter, body)
            beInt((COUNTER_SIZE + ciphertext.size).toLong()) + counter + ciphertext
        }

        fun seal(message: JSONObject): ByteArray = sealRaw(dumps(message))

        fun openRaw(body: ByteArray): ByteArray {
            val key = recvKey ?: throw WireError("frame received before the peer's preamble")
            if (body.size <= COUNTER_SIZE) throw WireError("frame too short")
            val counter = readBeInt(body, 0)
            if (counter != recvCounter + 1) throw WireError("frame counter did not advance; replay rejected")
            val plain = Crypto.open(key, peerPrefix!! + body.copyOfRange(0, COUNTER_SIZE), body.copyOfRange(COUNTER_SIZE, body.size))
            recvCounter = counter
            return plain
        }

        fun open(body: ByteArray): JSONObject = parse(openRaw(body))
    }

    // --- framing over a stream ---

    fun writeFrame(out: OutputStream, frame: ByteArray) {
        out.write(frame)
        out.flush()
    }

    /** One frame's sealed body, its length checked before anything is buffered. */
    fun readFrame(input: DataInputStream, limit: Int = MAX_FRAME_BYTES): ByteArray {
        val length = readBeInt(readExact(input, HEADER_SIZE), 0)
        if (length < MIN_FRAME_BYTES || length > limit) throw WireError("invalid message length: $length")
        return readExact(input, length.toInt())
    }

    fun readPreamble(input: DataInputStream): ByteArray {
        val head = readExact(input, PREAMBLE_HEAD_SIZE)
        if (!head.copyOfRange(0, 5).contentEquals(MAGIC)) throw VersionMismatch(null)
        val version = head[5].toInt() and 0xFF
        if (version != VERSION) {
            // An older responder answers 14 bytes and closes; draining them keeps the error clean.
            if (version < VERSION) runCatching { readExact(input, NONCE_PREFIX_SIZE) }
            throw VersionMismatch(version)
        }
        return head + readExact(input, PREAMBLE_SIZE - PREAMBLE_HEAD_SIZE)
    }

    private fun readExact(input: InputStream, count: Int): ByteArray {
        val out = ByteArray(count)
        var read = 0
        while (read < count) {
            val got = input.read(out, read, count - read)
            if (got < 0) throw EOFException("the link closed while reading")
            read += got
        }
        return out
    }

    fun beInt(value: Long): ByteArray = byteArrayOf(
        ((value shr 24) and 0xFF).toByte(), ((value shr 16) and 0xFF).toByte(),
        ((value shr 8) and 0xFF).toByte(), (value and 0xFF).toByte(),
    )

    fun readBeInt(bytes: ByteArray, offset: Int): Long {
        var value = 0L
        for (index in offset until offset + 4) value = (value shl 8) or (bytes[index].toLong() and 0xFF)
        return value
    }

    // --- messages ---

    fun dumps(message: JSONObject): ByteArray = message.toString().toByteArray(Charsets.UTF_8)

    fun parse(plain: ByteArray): JSONObject {
        var body = plain
        while (body.size >= 3 && body[0] == 0xEF.toByte() && body[1] == 0xBB.toByte() && body[2] == 0xBF.toByte()) {
            body = body.copyOfRange(3, body.size)
        }
        return try {
            JSONObject(String(body, Charsets.UTF_8))
        } catch (exc: Exception) {
            throw WireError("malformed message body")
        }
    }

    private fun message(type: String, data: JSONObject): JSONObject =
        JSONObject().put("type", type).put("data", data)

    fun hello(machineId: ByteArray, name: String, platform: String, app: String, caps: List<String>, port: Int): JSONObject {
        val data = JSONObject()
            .put("version", VERSION)
            .put("id", b64(machineId))
            .put("name", name.take(MAX_NAME_CHARS))
            .put("platform", platform)
            .put("app", app)
            .put("caps", JSONArray(caps))
            .put("port", port)
        return message(MSG_HELLO, data)
    }

    /** `focus` with an empty `reach`: a take that stays, with nowhere to hand on to. */
    fun focus(route: Int, target: ByteArray, resistancePx: Int = 0): JSONObject {
        val data = JSONObject()
            .put("route", route)
            .put("target", b64(target))
            .put("resistance_px", resistancePx)
            .put("reach", JSONArray())
        return message(MSG_FOCUS, data)
    }

    fun letGo(route: Int, target: ByteArray): JSONObject =
        message(MSG_FOCUS, JSONObject().put("route", route).put("target", b64(target)))

    fun ping(probe: Int? = null, echo: Int? = null): JSONObject {
        val data = JSONObject()
        if (probe != null) data.put("probe", probe)
        if (echo != null) data.put("echo", echo)
        return message(MSG_PING, data)
    }

    fun key(type: String, key: String, us: String? = null): JSONObject {
        val data = JSONObject().put("key", key)
        if (us != null) data.put("us", us)
        return message(type, data)
    }

    fun mouseMove(dx: Int, dy: Int): JSONObject = message(
        MSG_MOUSEMOVE,
        JSONObject().put("dx", dx.coerceIn(-MOVE_MAX, MOVE_MAX)).put("dy", dy.coerceIn(-MOVE_MAX, MOVE_MAX)),
    )

    fun mouseButton(type: String, button: String): JSONObject =
        message(type, JSONObject().put("button", button))

    fun scroll(dy: Double, dx: Double = 0.0, mode: String = "line"): JSONObject = message(
        MSG_SCROLL,
        JSONObject().put("dy", dy).put("dx", dx).put("mode", mode),
    )

    /**
     * The clipboard. Text only: Android's clipboard carries images as content URIs, and turning
     * one into a PNG is work this app does not do, so it never offers `clipboard_image` and so is
     * never sent one. The wire allows either part to be absent (WIRE.md section 3).
     */
    fun clipboard(text: String): JSONObject =
        message(MSG_CLIPBOARD, JSONObject().put("text", text))

    /** The text a `clipboard` carries, or null when there is none or it is over the limit. */
    fun readClipboardText(message: JSONObject): String? {
        val data = message.optJSONObject("data") ?: return null
        if (!data.has("text") || data.isNull("text")) return null
        val text = data.optString("text")
        if (text.isEmpty()) return null
        return if (text.toByteArray(Charsets.UTF_8).size <= CLIPBOARD_MAX_BYTES) text else null
    }

    /** Whether `text` is short enough to send. */
    fun clipboardFits(text: String): Boolean =
        text.isNotEmpty() && text.toByteArray(Charsets.UTF_8).size <= CLIPBOARD_MAX_BYTES

    fun text(value: String): JSONObject {
        var body = value
        while (body.toByteArray(Charsets.UTF_8).size > TEXT_MAX_BYTES) body = body.dropLast(1)
        return message(MSG_TEXT, JSONObject().put("text", body))
    }

    // --- readers ---

    class Welcome(
        val id: ByteArray?, val name: String, val platform: String, val app: String,
        val caps: List<String>, val accepts: Boolean, val error: String?,
    )

    fun readWelcome(message: JSONObject): Welcome {
        if (message.optString("type") != MSG_WELCOME) throw WireError("not a welcome")
        val data = message.optJSONObject("data") ?: throw WireError("malformed welcome")
        val error = data.optString("error", "").ifEmpty { null }
        if (error != null) return Welcome(null, "", "", "", emptyList(), false, error)
        val caps = mutableListOf<String>()
        data.optJSONArray("caps")?.let { array ->
            for (index in 0 until array.length()) caps.add(array.optString(index))
        }
        return Welcome(
            readId(if (data.has("id") && !data.isNull("id")) data.optString("id") else null),
            data.optString("name", ""),
            data.optString("platform", "windows"),
            data.optString("app", ""),
            caps,
            data.optBoolean("accepts", false),
            null,
        )
    }

    class Ack(val seq: Long, val heldUs: Long, val locked: Boolean)

    fun readAck(message: JSONObject): Ack {
        val data = message.optJSONObject("data") ?: throw WireError("malformed ack")
        if (!data.has("seq") || !data.has("held_us")) throw WireError("malformed ack")
        return Ack(data.optLong("seq", -1), data.optLong("held_us", -1), data.optBoolean("locked", false))
    }

    fun readRoute(message: JSONObject): Int? {
        val data = message.optJSONObject("data") ?: return null
        val route = data.optInt("route", -1)
        return if (route > 0) route else null
    }

    fun readRefuseWhy(message: JSONObject): String =
        message.optJSONObject("data")?.optString("why", "")?.ifEmpty { "no reason given" } ?: "no reason given"
}
