package com.capturekvm.wire

import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.ArrayDeque
import org.json.JSONObject

/**
 * The initiator half of one Beamer link, redialled until [stop].
 *
 * The same shape as `windows/capturekvm/link.py`: a worker that dials, handshakes and sends, and
 * a reader that owns `recv`, because the session's receive counter expects one reader. Liveness
 * follows WIRE.md section 9 -- ping after a second of quiet, give up at two, and give up on an
 * input unacknowledged for 1.5 seconds.
 *
 * [onEvent] is called from these threads, so a UI must post it to its own.
 */
class BeamerLink(
    private val peer: Pairing.Peer,
    private val machineId: ByteArray,
    private val name: String,
    private val platform: String = "android",
    private val app: String = "capturekvm",
    private val resistancePx: Int = 0,
    private val onEvent: (String, JSONObject) -> Unit = { _, _ -> },
) {

    companion object {
        const val PING_AFTER_MS = 1000L
        const val SILENCE_LIMIT_MS = 2000L
        const val UNACKED_LIMIT_MS = 1500L
        const val REDIAL_MS = 2000L
        const val CONNECT_TIMEOUT_MS = 1000
        const val HANDSHAKE_MS = 5000
        const val READ_TIMEOUT_MS = 250

        // A capability is a promise to handle what arrives. `clipboard` is offered because the
        // app sets Android's clipboard from it; `clipboard_image` is not, because it would then
        // be sent PNGs it has no way to put anywhere.
        val CAPS = listOf("ping_rtt", "clipboard", PadWire.CAP_GAMEPAD)

        const val STATE_DOWN = "down"
        const val STATE_DIALLING = "dialling"
        const val STATE_HANDSHAKE = "handshake"
        const val STATE_TAKING = "taking"
        const val STATE_UP = "up"
        const val STATE_STOPPED = "stopped"
    }

    private class LinkClosed(message: String) : Exception(message)

    @Volatile var state: String = STATE_DOWN; private set
    @Volatile var peerName: String = peer.name; private set
    @Volatile var peerPlatform: String = peer.platform; private set
    @Volatile var peerCaps: List<String> = emptyList(); private set
    @Volatile var peerAccepts: Boolean = false; private set
    @Volatile var rttMs: Double? = null; private set
    @Volatile var lastError: String? = null; private set

    private val peerId: ByteArray = Wire.readId(peer.id)
        ?: throw IllegalArgumentException("the stored PC has no usable machine id")

    @Volatile private var stopped = false
    private var worker: Thread? = null
    private var reader: Thread? = null
    @Volatile private var socket: Socket? = null
    @Volatile private var session: Wire.Session? = null
    @Volatile private var output: OutputStream? = null

    private val outbox = ArrayDeque<JSONObject>()
    private val outboxLock = Object()
    private var seq = 0L
    private val pending = ArrayDeque<Pair<Long, Long>>()
    private val pendingLock = Object()
    @Volatile private var lastRecv = 0L
    @Volatile private var lastSend = 0L
    @Volatile private var route = 0
    @Volatile private var accepted = false

    val isUp: Boolean get() = state == STATE_UP
    val gamepadReady: Boolean get() = isUp && peerCaps.contains(PadWire.CAP_GAMEPAD)

    // --- lifecycle ---

    fun start() {
        if (worker != null) return
        stopped = false
        worker = Thread({ run() }, "beamer-link").apply { isDaemon = true; start() }
    }

    fun stop() {
        stopped = true
        synchronized(outboxLock) { outboxLock.notifyAll() }
        closeSocket()
        worker?.join(3000)
        worker = null
        setState(STATE_STOPPED)
    }

    // --- sending ---

    /** An input message. The sequence number goes on as it leaves, so merged moves cost one. */
    fun sendInput(message: JSONObject) {
        if (state != STATE_UP && state != STATE_TAKING) return
        synchronized(outboxLock) {
            val last = outbox.peekLast()
            if (last != null && message.optString("type") == Wire.MSG_MOUSEMOVE &&
                last.optString("type") == Wire.MSG_MOUSEMOVE && !last.optJSONObject("data")!!.has("seq")
            ) {
                // Merge rather than queue: a backlog of moves is lag the user feels as drift.
                val into = last.getJSONObject("data")
                val from = message.getJSONObject("data")
                into.put("dx", (into.optInt("dx") + from.optInt("dx")).coerceIn(-Wire.MOVE_MAX, Wire.MOVE_MAX))
                into.put("dy", (into.optInt("dy") + from.optInt("dy")).coerceIn(-Wire.MOVE_MAX, Wire.MOVE_MAX))
            } else {
                outbox.addLast(message)
            }
            outboxLock.notifyAll()
        }
    }

    /** A pad frame, dropped unless the PC advertised the capability. */
    fun sendPad(message: JSONObject) {
        if (!gamepadReady) return
        synchronized(outboxLock) {
            // Replace a queued report for the same pad rather than queueing behind it: a report
            // is full state, so the newest says everything the older ones did, and sending them
            // in turn replays stick positions the player has already left.
            val pad = message.optJSONObject("data")?.optInt("pad", 0) ?: 0
            val existing = outbox.indexOfFirst {
                it.optString("type") == PadWire.MSG_REPORT &&
                    (it.optJSONObject("data")?.optInt("pad", 0) ?: 0) == pad &&
                    it.optJSONObject("data")?.has("seq") != true
            }
            if (existing >= 0) {
                val kept = outbox.toMutableList()
                kept[existing] = message
                outbox.clear(); outbox.addAll(kept)
            } else {
                outbox.addLast(message)
            }
            outboxLock.notifyAll()
        }
    }

    /**
     * This device's clipboard, put at the front of the queue: WIRE.md section 3 has the owner send
     * its clipboard after the taking `focus` and **before any input**, so a paste on the PC finds
     * what was copied here.
     */
    fun sendClipboard(text: String): Boolean {
        if (!isUp || !peerCaps.contains("clipboard")) return false
        if (!Wire.clipboardFits(text)) return false
        synchronized(outboxLock) { outbox.addFirst(Wire.clipboard(text)); outboxLock.notifyAll() }
        return true
    }

    fun sendUnsequenced(message: JSONObject) {
        synchronized(outboxLock) { outbox.addLast(message); outboxLock.notifyAll() }
    }

    // --- the worker ---

    private fun run() {
        while (!stopped) {
            try {
                connectOnce()
            } catch (exc: Wire.VersionMismatch) {
                fail("the PC speaks link version ${exc.peer}, this app speaks ${Wire.VERSION}")
            } catch (exc: LinkClosed) {
                fail(exc.message ?: "the link closed")
            } catch (exc: Crypto.CryptoRefused) {
                fail("the PC refused this pairing; pair again")
            } catch (exc: Exception) {
                fail("could not reach ${peer.host}:${peer.port} (${exc.message ?: exc.javaClass.simpleName})")
            } finally {
                teardown()
            }
            if (stopped) break
            try {
                Thread.sleep(REDIAL_MS)
            } catch (exc: InterruptedException) {
                break
            }
        }
        setState(STATE_STOPPED)
    }

    private fun connectOnce() {
        setState(STATE_DIALLING)
        val connection = Socket()
        connection.connect(InetSocketAddress(peer.host, peer.port), CONNECT_TIMEOUT_MS)
        connection.tcpNoDelay = true
        socket = connection
        output = connection.getOutputStream()
        val input = DataInputStream(connection.getInputStream().buffered())

        handshake(connection, input)

        // The reader starts before the focus goes out: the accept that answers it arrives on this
        // socket, and with no reader running nothing would take it and the take would time out.
        lastRecv = System.currentTimeMillis()
        lastSend = lastRecv
        reader = Thread({ readLoop(connection, input) }, "beamer-reader").apply { isDaemon = true; start() }

        takeInput()
        sendLoop()
    }

    private fun handshake(connection: Socket, input: DataInputStream) {
        setState(STATE_HANDSHAKE)
        connection.soTimeout = HANDSHAKE_MS
        val fresh = Wire.Session(peer.token)
        val out = output ?: throw LinkClosed("the link closed while connecting")
        Wire.writeFrame(out, fresh.preamble())
        fresh.acceptPreamble(Wire.readPreamble(input))
        session = fresh

        Wire.writeFrame(out, fresh.seal(Wire.hello(machineId, name, platform, app, CAPS, port = 0)))
        val welcome = Wire.readWelcome(fresh.open(Wire.readFrame(input, Wire.HELLO_MAX_BYTES)))
        if (welcome.error != null) throw LinkClosed("the PC refused the hello: ${welcome.error}")
        if (welcome.id == null || !welcome.id.contentEquals(peerId)) {
            throw LinkClosed("another machine answered at this address; pair again or fix the address")
        }
        peerName = welcome.name
        peerPlatform = welcome.platform
        peerCaps = welcome.caps
        peerAccepts = welcome.accepts
        emit("welcome", JSONObject()
            .put("name", welcome.name).put("platform", welcome.platform).put("app", welcome.app)
            .put("accepts", welcome.accepts).put("gamepad", welcome.caps.contains(PadWire.CAP_GAMEPAD)))
        if (!welcome.accepts) {
            throw LinkClosed("$peerName is not letting this app drive it (turn that on in Beamer)")
        }
    }

    private fun takeInput() {
        setState(STATE_TAKING)
        route += 1
        accepted = false
        val out = output ?: throw LinkClosed("the link closed while connecting")
        val current = session ?: throw LinkClosed("the link closed while connecting")
        Wire.writeFrame(out, current.seal(Wire.focus(route, peerId, resistancePx)))
        val until = System.currentTimeMillis() + 2000
        while (!accepted && System.currentTimeMillis() < until) {
            if (stopped) throw LinkClosed("stopped")
            Thread.sleep(10)
        }
        if (!accepted) throw LinkClosed("the PC did not answer the request to take its keyboard and mouse")
        setState(STATE_UP)
    }

    private fun sendLoop() {
        while (!stopped) {
            val message = nextMessage()
            val now = System.currentTimeMillis()
            if (message == null) {
                checkLiveness(now)
                if (now - lastSend >= PING_AFTER_MS) write(Wire.ping(probe = (now and 0x7FFFFFFF).toInt()))
                continue
            }
            val type = message.optString("type")
            val numbered = Wire.INPUT_TYPES.contains(type) ||
                (type == PadWire.MSG_REPORT && peerCaps.contains(PadWire.CAP_GAMEPAD))
            if (numbered) {
                seq += 1
                message.getJSONObject("data").put("seq", seq)
                synchronized(pendingLock) { pending.addLast(seq to now) }
            }
            write(message)
            checkLiveness(now)
        }
    }

    private fun nextMessage(): JSONObject? = synchronized(outboxLock) {
        if (outbox.isEmpty()) outboxLock.wait(50)
        outbox.pollFirst()
    }

    private fun write(message: JSONObject) {
        val out = output ?: throw LinkClosed("the link closed while sending")
        val current = session ?: throw LinkClosed("the link closed while sending")
        try {
            Wire.writeFrame(out, current.seal(message))
        } catch (exc: Exception) {
            throw LinkClosed("the link closed while sending: ${exc.message}")
        }
        lastSend = System.currentTimeMillis()
    }

    private fun checkLiveness(now: Long) {
        if (now - lastRecv > SILENCE_LIMIT_MS) throw LinkClosed("${peerName.ifEmpty { "the PC" }} went quiet")
        val oldest = synchronized(pendingLock) { pending.peekFirst()?.second }
        if (oldest != null && now - oldest > UNACKED_LIMIT_MS) {
            throw LinkClosed("the PC stopped acknowledging input")
        }
    }

    // --- the reader ---

    private fun readLoop(connection: Socket, input: DataInputStream) {
        try {
            connection.soTimeout = READ_TIMEOUT_MS
            while (!stopped) {
                val frame = try {
                    Wire.readFrame(input)
                } catch (exc: SocketTimeoutException) {
                    continue
                }
                lastRecv = System.currentTimeMillis()
                handle(session?.open(frame) ?: break)
            }
        } catch (exc: Exception) {
            fail("the link dropped: ${exc.message ?: exc.javaClass.simpleName}")
        } finally {
            closeSocket()
        }
    }

    private fun handle(message: JSONObject) {
        when (message.optString("type")) {
            Wire.MSG_ACK -> {
                val ack = Wire.readAck(message)
                var sentAt: Long? = null
                synchronized(pendingLock) {
                    val keep = ArrayDeque<Pair<Long, Long>>()
                    for (entry in pending) {
                        if (entry.first == ack.seq) sentAt = entry.second
                        if (entry.first > ack.seq) keep.addLast(entry)
                    }
                    pending.clear()
                    pending.addAll(keep)
                }
                sentAt?.let {
                    // held_us is the PC's own hold; what is left is the network.
                    rttMs = maxOf(0.0, (System.currentTimeMillis() - it).toDouble() - ack.heldUs / 1000.0)
                }
            }
            Wire.MSG_ACCEPT -> if (Wire.readRoute(message) == route) accepted = true
            Wire.MSG_REFUSE -> {
                val why = Wire.readRefuseWhy(message)
                emit("refused", JSONObject().put("why", why))
                fail("the PC refused: ${refusalText(why)}")
                closeSocket()
            }
            Wire.MSG_ACCEPTS -> {
                val data = message.optJSONObject("data")
                peerAccepts = data?.optBoolean("accepts", false) ?: false
                emit("accepts", JSONObject().put("accepts", peerAccepts))
                if (!peerAccepts) closeSocket()
            }
            Wire.MSG_PING -> {
                val probe = message.optJSONObject("data")?.optInt("probe", 0) ?: 0
                if (probe > 0) sendUnsequenced(Wire.ping(echo = probe))
            }
            Wire.MSG_CLIPBOARD -> {
                val text = Wire.readClipboardText(message)
                // Handed to the listener: Android's clipboard wants the main thread.
                if (text != null) emit("clipboard", JSONObject().put("text", text))
            }
            Wire.MSG_SWITCH -> emit("switch", JSONObject())
        }
    }

    // --- plumbing ---

    private fun teardown() {
        closeSocket()
        reader?.let { if (it !== Thread.currentThread()) it.join(1000) }
        reader = null
        session = null
        output = null
        synchronized(outboxLock) { outbox.clear() }
        synchronized(pendingLock) { pending.clear() }
        seq = 0
        if (state != STATE_STOPPED) setState(STATE_DOWN)
    }

    private fun closeSocket() {
        val open = socket
        socket = null
        runCatching { open?.close() }
    }

    private fun setState(next: String) {
        if (state != next) {
            state = next
            emit("state", JSONObject().put("state", next))
        }
    }

    private fun fail(text: String) {
        if (lastError != text) {
            lastError = text
            emit("error", JSONObject().put("text", text))
        }
    }

    private fun emit(kind: String, detail: JSONObject) {
        runCatching { onEvent(kind, detail) }
    }

    private fun refusalText(why: String): String = when (why) {
        "owned" -> "another machine is driving it"
        "not_allowed" -> "it is not set to let this app drive it"
        "busy" -> "it is busy"
        "sent_home" -> "it sent input back to itself"
        "malformed" -> "it did not understand the request"
        else -> why
    }
}
