package com.capturekvm.wire

import java.io.DataInputStream
import java.io.OutputStream
import java.math.BigInteger
import java.net.InetSocketAddress
import java.net.Socket
import org.json.JSONObject

/**
 * Pairing version 3: the CPace exchange that turns six digits into a link token.
 *
 * CPace is a password-authenticated key exchange, so a wrong code agrees nothing and tells a
 * listener nothing; the price is that every byte of the transcript must match the other side's
 * exactly. Everything here is transcribed from `core/pairing.py` and checked against values the
 * Python produced (`PairingVectorsTest`).
 *
 * This app pairs over TCP to the PC's pairing port -- Beamer calls it "pair by address" -- which
 * needs no UDP beacon and works across subnets where a broadcast would not.
 */
object Pairing {

    const val PAIRING_V3 = 3
    const val CODE_DIGITS = 6
    const val NONCE_BYTES = 16
    const val SHARE_BYTES = 32
    const val ID_BYTES = 16
    const val MAX_PEERS = 32
    const val MAX_DATAGRAM_BYTES = 1024
    const val MAX_PAIR_ID_CHARS = 64
    const val MAX_NAME_CHARS = 48

    const val CONNECT_TIMEOUT_MS = 3000
    const val EXCHANGE_TIMEOUT_MS = 10000

    private val DSI = "CPace255".toByteArray(Charsets.US_ASCII)
    private val CHANNEL_V3 = "beamer-pair-v3".toByteArray(Charsets.US_ASCII)
    private val TOKEN_INFO_V3 = "beamer-pair-token-v3".toByteArray(Charsets.US_ASCII)
    private val DONE_LABEL = "beamer-pair-done".toByteArray(Charsets.US_ASCII)
    private val ROLE_REQUESTER = "requester".toByteArray(Charsets.US_ASCII)
    private val ROLE_HOST = "host".toByteArray(Charsets.US_ASCII)

    private const val SHA512_BLOCK_BYTES = 128

    const val MSG_BEACON = "beacon"
    const val MSG_PAIR_START = "pair_start"
    const val MSG_PAIR_ANSWER = "pair_answer"
    const val MSG_PAIR_CONFIRM = "pair_confirm"
    const val MSG_PAIR_DONE = "pair_done"

    class PairingError(message: String) : Exception(message)
    class AlreadyPaired(message: String) : Exception(message)

    // --- the draft's byte plumbing ---

    /** `prepend_len`: the length as LEB128, then the bytes. */
    fun prependLen(data: ByteArray): ByteArray {
        val out = ArrayList<Byte>(data.size + 2)
        var length = data.size
        while (true) {
            out.add(if (length < 128) (length and 0x7F).toByte() else ((length and 0x7F) or 0x80).toByte())
            length = length shr 7
            if (length == 0) break
        }
        return out.toByteArray() + data
    }

    fun lvCat(vararg parts: ByteArray): ByteArray {
        var out = ByteArray(0)
        for (part in parts) out += prependLen(part)
        return out
    }

    /** `calculate_generator` for G_X25519 with SHA-512. */
    fun generator(prs: ByteArray, channel: ByteArray, sid: ByteArray): ByteArray {
        val padLength = maxOf(0, SHA512_BLOCK_BYTES - 1 - prependLen(prs).size - prependLen(DSI).size)
        val digest = Crypto.sha512(lvCat(DSI, prs, ByteArray(padLength), channel, sid)).copyOf(SHARE_BYTES)
        val mask = BigInteger.TWO.pow(255).subtract(BigInteger.ONE)
        val r = Crypto.fromLittleEndian(digest).and(mask)
        return Crypto.littleEndian(Crypto.elligator2(r), SHARE_BYTES)
    }

    fun sessionId(pairId: String, nonce: ByteArray): ByteArray =
        lvCat(pairId.toByteArray(Charsets.UTF_8), nonce)

    /** CPace's ADa or ADb. Version 3 binds the platform and link port in too. */
    fun party(role: ByteArray, identity: ByteArray, name: String, platform: String, port: Int): ByteArray =
        lvCat(
            role, identity, name.toByteArray(Charsets.UTF_8), platform.toByteArray(Charsets.US_ASCII),
            byteArrayOf(((port shr 8) and 0xFF).toByte(), (port and 0xFF).toByte()),
        )

    fun share(code: String, sid: ByteArray, scalar: ByteArray, secret: ByteArray = ByteArray(0)): ByteArray =
        Crypto.x25519(scalar, generator(code.toByteArray(Charsets.UTF_8) + secret, CHANNEL_V3, sid))

    class Keys(val token: String, val requesterTag: ByteArray, val hostTag: ByteArray, val doneTag: ByteArray)

    fun keys(sid: ByteArray, shared: ByteArray, ya: ByteArray, ada: ByteArray, yb: ByteArray, adb: ByteArray): Keys {
        val isk = Crypto.sha512(
            lvCat(DSI + "_ISK".toByteArray(Charsets.US_ASCII), sid, shared) + lvCat(ya, ada) + lvCat(yb, adb)
        )
        val macKey = Crypto.sha512("CPaceMac".toByteArray(Charsets.US_ASCII) + sid + isk)
        fun tag(message: ByteArray) = Crypto.hmacSha512(macKey, message)
        return Keys(
            Wire.b64(Crypto.hkdfSha256(isk, sid, TOKEN_INFO_V3)),
            tag(lvCat(ya, ada)),
            tag(lvCat(yb, adb)),
            tag(lvCat(DONE_LABEL)),
        )
    }

    // --- the exchange ---

    /** What pairing stores about a PC: the same fields `core.pairing.peer_entry` writes. */
    class Peer(
        val id: String, val name: String, val platform: String, val token: String,
        val host: String, val port: Int,
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("id", id).put("name", name).put("platform", platform).put("token", token)
            .put("host", host).put("port", port)
            .put("paired_at", System.currentTimeMillis() / 1000)

        companion object {
            fun fromJson(json: JSONObject) = Peer(
                json.optString("id"), json.optString("name"),
                json.optString("platform", "windows"), json.optString("token"),
                json.optString("host"), json.optInt("port", Wire.DEFAULT_PORT),
            )
        }
    }

    /**
     * Pairs with the PC at [address] showing [code]. Blocking; call it off the main thread.
     *
     * Only one answer is ever judged, which is the rule that limits whoever is answering to a
     * single guess at the code. A failure sends a confirmation that cannot pass, so the PC drops
     * the code instead of leaving it live for the rest of its minute.
     */
    fun pairByAddress(
        address: String, code: String, machineId: ByteArray, name: String,
        platform: String = "android", port: Int = Wire.PAIRING_PORT, ourLinkPort: Int = 0,
    ): Peer {
        val digits = code.filter { it.isDigit() }
        if (digits.length != CODE_DIGITS) throw PairingError("the code is $CODE_DIGITS digits")
        if (machineId.size != ID_BYTES || machineId.all { it == 0.toByte() }) {
            throw PairingError("this app has no usable machine id")
        }

        val socket = Socket()
        try {
            socket.connect(InetSocketAddress(address, port), CONNECT_TIMEOUT_MS)
        } catch (exc: Exception) {
            throw PairingError("nothing answered on $address:$port -- is Beamer showing a code?")
        }
        socket.use {
            socket.soTimeout = EXCHANGE_TIMEOUT_MS
            socket.tcpNoDelay = true
            val input = DataInputStream(socket.getInputStream().buffered())
            val output = socket.getOutputStream()

            val beacon = recvFrame(input)
            if (beacon.optString("type") != MSG_BEACON) throw PairingError("that port answered, but not as Beamer's pairing")
            if (beacon.optInt("pairing", -1) != PAIRING_V3) {
                throw PairingError("the PC pairs with a different version of the exchange")
            }
            val pairId = beacon.optString("pair", "")
            if (pairId.isEmpty() || pairId.length > MAX_PAIR_ID_CHARS) {
                throw PairingError("the PC is not showing a pairing code right now")
            }

            val nonce = Crypto.randomBytes(NONCE_BYTES)
            val scalar = Crypto.randomBytes(SHARE_BYTES)
            val sid = sessionId(pairId, nonce)
            val ourShare = share(digits, sid, scalar)
            val shortName = name.take(MAX_NAME_CHARS)

            sendFrame(output, JSONObject()
                .put("type", MSG_PAIR_START).put("pair", pairId).put("v", PAIRING_V3)
                .put("nonce", Wire.b64(nonce)).put("share", Wire.b64(ourShare))
                .put("name", shortName).put("id", Wire.b64(machineId))
                .put("platform", platform).put("port", ourLinkPort))

            val answer = recvFrame(input)
            if (answer.optString("type") != MSG_PAIR_ANSWER) throw PairingError("the PC did not answer the pairing request")
            if (!answer.optBoolean("ok", false)) {
                throw PairingError(errorText(answer.optString("error", "refused")))
            }

            val hostShare = Wire.unb64(answer.optString("share", ""))
            val hostTagSent = Wire.unb64(answer.optString("tag", ""))
            val hostId = Wire.unb64(answer.optString("id", ""))
            val hostName = answer.optString("name", "")
            val hostPlatform = answer.optString("platform", "")
            val hostPort = answer.optInt("port", -1)
            if (hostShare.size != SHARE_BYTES || hostId.size != ID_BYTES || hostName.isEmpty() ||
                hostName.length > MAX_NAME_CHARS || hostPlatform.isEmpty() || hostPort !in 0..65535
            ) {
                abort(output, pairId)
                throw PairingError(errorText("refused"))
            }

            val requester = party(ROLE_REQUESTER, machineId, shortName, platform, ourLinkPort)
            val host = party(ROLE_HOST, hostId, hostName, hostPlatform, hostPort)
            val shared = try {
                Crypto.x25519(scalar, hostShare)
            } catch (exc: Crypto.CryptoRefused) {
                abort(output, pairId)
                throw PairingError(errorText("refused"))
            }
            val agreed = keys(sid, shared, ourShare, requester, hostShare, host)
            if (!Crypto.constantTimeEquals(hostTagSent, agreed.hostTag)) {
                // The one guess this attempt allows has now been spent.
                abort(output, pairId)
                throw PairingError(errorText("refused"))
            }

            sendFrame(output, JSONObject()
                .put("type", MSG_PAIR_CONFIRM).put("pair", pairId).put("tag", Wire.b64(agreed.requesterTag)))

            val done = recvFrame(input)
            if (done.optString("type") != MSG_PAIR_DONE || !done.optBoolean("ok", false)) {
                throw PairingError(errorText(done.optString("error", "refused")))
            }
            if (!Crypto.constantTimeEquals(Wire.unb64(done.optString("tag", "")), agreed.doneTag)) {
                throw PairingError(errorText("refused"))
            }

            return Peer(Wire.b64(hostId), hostName, hostPlatform, agreed.token, address, hostPort)
        }
    }

    private fun abort(output: OutputStream, pairId: String) {
        runCatching {
            sendFrame(output, JSONObject().put("type", MSG_PAIR_CONFIRM).put("pair", pairId).put("tag", ""))
        }
    }

    // --- framing: a 4-byte big-endian length, then the JSON a datagram would carry ---

    private fun sendFrame(output: OutputStream, message: JSONObject) {
        // "beamy" is a wire constant kept at the pre-rename name; the PC checks for it.
        val body = JSONObject().put("beamy", 1)
        for (key in message.keys()) body.put(key, message.get(key))
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        if (bytes.size > MAX_DATAGRAM_BYTES) throw PairingError("the pairing message is too long")
        output.write(Wire.beInt(bytes.size.toLong()))
        output.write(bytes)
        output.flush()
    }

    private fun recvFrame(input: DataInputStream): JSONObject {
        val header = ByteArray(4)
        input.readFully(header)
        val length = Wire.readBeInt(header, 0)
        if (length <= 0 || length > MAX_DATAGRAM_BYTES) throw PairingError("the PC sent a pairing message of $length bytes")
        val body = ByteArray(length.toInt())
        input.readFully(body)
        return try {
            JSONObject(String(body, Charsets.UTF_8))
        } catch (exc: Exception) {
            throw PairingError("the PC sent something that was not a pairing message")
        }
    }

    fun errorText(reason: String): String = when (reason) {
        "not_pairing" -> "the PC is not showing a pairing code right now"
        "refused" -> "the code did not match -- ask the PC for a fresh one"
        "version" -> "the PC's Beamer pairs with a different version"
        "known" -> "this app is paired with that PC already"
        "full" -> "there is no room for another PC"
        else -> reason
    }
}
