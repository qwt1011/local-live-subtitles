let recorder;
let stream;
let audioContext;
let captureLanguage = 'en';

chrome.runtime.onMessage.addListener(async (message) => {
  if (message.type === 'offscreen-start') await start(message.streamId, message.language);
  if (message.type === 'offscreen-stop') stop();
});

async function start(streamId, language) {
  captureLanguage = language || 'en';
  stream = await navigator.mediaDevices.getUserMedia({audio: {mandatory: {chromeMediaSource: 'tab', chromeMediaSourceId: streamId}}, video: false});
  audioContext = new AudioContext();
  audioContext.createMediaStreamSource(stream).connect(audioContext.destination);
  startRecorder();
}

function startRecorder() {
  const currentChunks = [];
  const currentRecorder = new MediaRecorder(stream, {mimeType: 'audio/webm;codecs=opus'});
  recorder = currentRecorder;
  currentRecorder.ondataavailable = (event) => { if (event.data.size) currentChunks.push(event.data); };
  currentRecorder.onstop = () => {
    if (stream && stream.active) startRecorder();
    if (currentChunks.length) processRecording(new Blob(currentChunks, {type: 'audio/webm'}));
  };
  currentRecorder.start();
  setTimeout(() => { if (currentRecorder.state === 'recording') currentRecorder.stop(); }, 2000);
}

async function processRecording(blob) {
  try {
      const buffer = await blob.arrayBuffer();
      const response = await fetch(`http://127.0.0.1:8765/transcribe?language=${captureLanguage}`, {method: 'POST', body: buffer, headers: {'Content-Type': 'audio/webm'}});
      const data = await response.json();
      const text = (data.segments || []).map((item) => item.text).join(' ').trim();
      console.log('Local subtitle response', {text, translation: data.translation || '', data});
      if (text) chrome.runtime.sendMessage({type: 'subtitle', original: text, translation: data.translation || ''});
  } catch (error) {
      console.error('Local subtitle request failed', error);
      chrome.runtime.sendMessage({type: 'capture-error', error: String(error)});
  }
}

function stop() {
  if (recorder && recorder.state !== 'inactive') recorder.stop();
  if (stream) stream.getTracks().forEach((track) => track.stop());
  if (audioContext) audioContext.close();
}
