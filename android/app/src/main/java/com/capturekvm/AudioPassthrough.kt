package com.capturekvm

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Build
import android.util.Log
import kotlin.concurrent.thread

/**
 * The capture card's sound, played on this device.
 *
 * A capture card carries the PC's HDMI audio as a USB audio device, separate from its video. This
 * reads that input and writes it straight to the speakers or headphones. **Nothing goes over the
 * network** -- the sound arrives on the same cable as the picture, so the two stay in step and the
 * Beamer link carries only key presses.
 *
 * One thread, one buffer, no queue: a read of N frames is written as N frames. Nothing is
 * resampled or re-mixed, because the way audio ends up a quarter-second behind the picture is a
 * buffer somewhere quietly absorbing a mismatch.
 *
 * `MediaRecorder.AudioSource.UNPROCESSED` is asked for first. The default source applies the
 * platform's voice processing -- automatic gain, noise suppression, sometimes an echo canceller --
 * which is right for a phone call and wrong for a game soundtrack.
 */
class AudioPassthrough(
    private val context: Context,
    private val onStatus: (String) -> Unit = {},
) {

    companion object {
        private const val TAG = "capturekvm.audio"

        /** Tried in order; the first the card accepts wins. */
        private val SAMPLE_RATES = intArrayOf(48000, 44100, 32000, 16000)

        /** Rounded up to the device's own minimum. Small keeps the sound close to the picture. */
        private const val WANTED_FRAMES = 256

        private val USB_TYPES = setOf(
            AudioDeviceInfo.TYPE_USB_DEVICE,
            AudioDeviceInfo.TYPE_USB_HEADSET,
            AudioDeviceInfo.TYPE_USB_ACCESSORY,
        )
    }

    private var worker: Thread? = null
    @Volatile private var running = false
    private var record: AudioRecord? = null
    private var track: AudioTrack? = null

    @Volatile var format: String? = null; private set
    @Volatile var underruns: Int = 0; private set

    val isRunning: Boolean get() = running

    private val audioManager: AudioManager
        get() = context.getSystemService(AudioManager::class.java)

    /** The card's audio input, if Android can see one. */
    fun usbInput(): AudioDeviceInfo? =
        audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { it.type in USB_TYPES }

    /** A line for the UI when there is no card audio to play. */
    fun describeInputs(): String {
        val devices = audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)
        if (devices.isEmpty()) return "no audio inputs at all"
        return devices.joinToString(", ") { "${it.productName} (type ${it.type})" }
    }

    /**
     * Starts the passthrough. Returns false and reports why when it cannot start.
     *
     * The caller must already hold RECORD_AUDIO; this does not ask for it, because a permission
     * dialog belongs to an activity and not to an audio engine.
     */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        stop()
        val input = usbInput()
        if (input == null) {
            onStatus("no USB audio from the card -- Android sees: ${describeInputs()}")
            return false
        }

        for (rate in SAMPLE_RATES) {
            val built = open(rate, input) ?: continue
            val (recorder, player, frames) = built
            record = recorder
            track = player
            format = "${rate / 1000} kHz stereo, $frames-frame buffer"
            running = true
            underruns = 0
            worker = thread(name = "audio-passthrough", isDaemon = true) { pump(recorder, player, frames) }
            onStatus("sound: $format from ${input.productName}")
            Log.i(TAG, "passthrough open at $rate Hz, $frames frames")
            return true
        }
        onStatus("the card's audio would not open at any rate this device supports")
        return false
    }

    /** (record, track, frames) or null when this rate does not work. */
    @SuppressLint("MissingPermission")
    private fun open(rate: Int, input: AudioDeviceInfo): Triple<AudioRecord, AudioTrack, Int>? {
        // Stereo in and out: a capture card's HDMI audio is two channels, and asking for mono
        // would make the platform down-mix, which is a resample by another name.
        val inMask = AudioFormat.CHANNEL_IN_STEREO
        val outMask = AudioFormat.CHANNEL_OUT_STEREO
        val encoding = AudioFormat.ENCODING_PCM_16BIT

        val minIn = AudioRecord.getMinBufferSize(rate, inMask, encoding)
        val minOut = AudioTrack.getMinBufferSize(rate, outMask, encoding)
        if (minIn <= 0 || minOut <= 0) return null

        // Bytes per frame: 2 channels x 2 bytes.
        val bytesPerFrame = 4
        val frames = maxOf(WANTED_FRAMES, minIn / bytesPerFrame, minOut / bytesPerFrame)
        val bufferBytes = frames * bytesPerFrame

        var recorder: AudioRecord? = null
        var player: AudioTrack? = null
        try {
            recorder = AudioRecord.Builder()
                .setAudioSource(
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) MediaRecorder.AudioSource.UNPROCESSED
                    else MediaRecorder.AudioSource.MIC
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(encoding)
                        .setSampleRate(rate)
                        .setChannelMask(inMask)
                        .build()
                )
                .setBufferSizeInBytes(bufferBytes * 2)
                .build()
            if (recorder.state != AudioRecord.STATE_INITIALIZED) throw IllegalStateException("record not initialised")
            // Pin it to the card rather than the built-in microphone.
            if (!recorder.setPreferredDevice(input)) {
                Log.i(TAG, "setPreferredDevice refused; the platform will choose the input")
            }

            player = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        // A game soundtrack, not a notification: MEDIA keeps it on the music
                        // stream and out of the way of the platform's ducking.
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(encoding)
                        .setSampleRate(rate)
                        .setChannelMask(outMask)
                        .build()
                )
                .setBufferSizeInBytes(bufferBytes * 2)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .apply {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                    }
                }
                .build()
            if (player.state != AudioTrack.STATE_INITIALIZED) throw IllegalStateException("track not initialised")

            recorder.startRecording()
            if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                throw IllegalStateException("recording did not start")
            }
            player.play()
            return Triple(recorder, player, frames)
        } catch (exc: Exception) {
            Log.i(TAG, "rate $rate did not open: ${exc.message}")
            runCatching { recorder?.release() }
            runCatching { player?.release() }
            return null
        }
    }

    private fun pump(recorder: AudioRecord, player: AudioTrack, frames: Int) {
        val buffer = ShortArray(frames * 2)
        while (running) {
            val read = try {
                recorder.read(buffer, 0, buffer.size)
            } catch (exc: Exception) {
                Log.i(TAG, "read failed: ${exc.message}")
                break
            }
            if (read <= 0) {
                // ERROR_INVALID_OPERATION and friends mean the card is gone, not a quiet moment.
                if (read < 0) break
                continue
            }
            var written = 0
            while (written < read && running) {
                val n = try {
                    player.write(buffer, written, read - written, AudioTrack.WRITE_BLOCKING)
                } catch (exc: Exception) {
                    Log.i(TAG, "write failed: ${exc.message}")
                    return
                }
                if (n <= 0) {
                    underruns += 1
                    break
                }
                written += n
            }
        }
    }

    fun stop() {
        running = false
        worker?.join(500)
        worker = null
        record?.let { runCatching { it.stop() }; runCatching { it.release() } }
        track?.let { runCatching { it.pause() }; runCatching { it.flush() }; runCatching { it.release() } }
        record = null
        track = null
        format = null
    }
}
