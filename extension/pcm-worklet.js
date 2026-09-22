/**
 * AudioWorklet：把当前标签页的音频原样交出来。
 *
 * 用 AudioWorklet 而不是 MediaRecorder，是为了拿到**裸 PCM**：
 * 原来的 MediaRecorder 路线是"每 2 秒独立编码一段 WebM → HTTP 上传 → 服务端落临时文件 → ffmpeg 解码"，
 * 既把分段决策放错在客户端（ARCHITECTURE_REVIEW.md S1），又逼着服务端反复解码重采样。
 * 现在客户端只负责把采样交出去，怎么分段完全由服务端决定。
 *
 * 注意：process() 返回后输入缓冲区会被复用，必须 slice() 复制再 postMessage。
 */
class PcmTap extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length) {
      this.port.postMessage(input[0].slice(0));
    }
    return true;
  }
}

registerProcessor('pcm-tap', PcmTap);
