package com.capturekvm

import android.content.Context
import com.capturekvm.wire.Crypto
import com.capturekvm.wire.Pairing
import com.capturekvm.wire.Wire
import org.json.JSONArray

/**
 * This device's machine id and the PCs it has paired with, in shared preferences.
 *
 * The machine id is drawn once and kept: it is half of what the PC's stored entry identifies this
 * app by, so losing it means pairing again. The token is the pairing secret, so this file is as
 * sensitive as a password store -- app-private storage, never backed up (see the manifest's
 * `allowBackup="false"`).
 */
class PeerStore(context: Context) {

    private val preferences = context.getSharedPreferences("capturekvm", Context.MODE_PRIVATE)

    val machineId: ByteArray
        get() {
            preferences.getString(KEY_MACHINE_ID, null)?.let { stored ->
                Wire.readId(stored)?.let { return it }
            }
            val fresh = Crypto.randomBytes(Wire.MACHINE_ID_SIZE)
            preferences.edit().putString(KEY_MACHINE_ID, Wire.b64(fresh)).apply()
            return fresh
        }

    var name: String
        get() = preferences.getString(KEY_NAME, null) ?: defaultName()
        set(value) = preferences.edit().putString(KEY_NAME, value.take(Wire.MAX_NAME_CHARS)).apply()

    fun peers(): List<Pairing.Peer> {
        val raw = preferences.getString(KEY_PEERS, null) ?: return emptyList()
        val array = try { JSONArray(raw) } catch (exc: Exception) { return emptyList() }
        return (0 until array.length()).mapNotNull { index ->
            array.optJSONObject(index)?.let { entry ->
                val peer = Pairing.Peer.fromJson(entry)
                if (Wire.readId(peer.id) != null && Wire.isPairedToken(peer.token) &&
                    peer.host.isNotEmpty() && peer.port > 0
                ) peer else null
            }
        }
    }

    /** Stores a freshly paired PC; an entry for the same machine replaces the old one. */
    fun add(peer: Pairing.Peer) {
        val kept = peers().filter { it.id != peer.id }
        if (kept.size >= Pairing.MAX_PEERS) throw IllegalStateException("no room for another PC")
        write(kept + peer)
    }

    fun forget(id: String) = write(peers().filter { it.id != id })

    var selectedId: String?
        get() = preferences.getString(KEY_SELECTED, null)
        set(value) = preferences.edit().putString(KEY_SELECTED, value).apply()

    fun selected(): Pairing.Peer? {
        val all = peers()
        return all.firstOrNull { it.id == selectedId } ?: all.firstOrNull()
    }

    var mouseScale: Float
        get() = preferences.getFloat(KEY_MOUSE_SCALE, 1.6f)
        set(value) = preferences.edit().putFloat(KEY_MOUSE_SCALE, value).apply()

    /** Whether the toolbar is hidden, so a tablet kept for playing on opens straight to picture. */
    var barHidden: Boolean
        get() = preferences.getBoolean(KEY_BAR_HIDDEN, false)
        set(value) = preferences.edit().putBoolean(KEY_BAR_HIDDEN, value).apply()

    private fun write(peers: List<Pairing.Peer>) {
        val array = JSONArray()
        peers.forEach { array.put(it.toJson()) }
        preferences.edit().putString(KEY_PEERS, array.toString()).apply()
    }

    private fun defaultName(): String {
        val model = android.os.Build.MODEL ?: "Android"
        return model.take(Wire.MAX_NAME_CHARS).ifEmpty { "Android" }
    }

    private companion object {
        const val KEY_MACHINE_ID = "machine_id"
        const val KEY_NAME = "name"
        const val KEY_PEERS = "peers"
        const val KEY_SELECTED = "selected"
        const val KEY_MOUSE_SCALE = "mouse_scale"
        const val KEY_BAR_HIDDEN = "bar_hidden"
    }
}
