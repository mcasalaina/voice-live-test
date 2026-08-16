const $ = (id) => document.getElementById(id);
let socket;
let audioContext;
let mediaStream;
let source;
let processor;
let playbackTime = 0;
let toolActive = false;

function stamp() {
  return new Date().toLocaleTimeString([], { hour12: false });
}

function addEntry(target, kind, text) {
  const box = $(target);
  if (box.classList.contains("empty")) {
    box.textContent = "";
    box.classList.remove("empty");
  }
  const row = document.createElement("div");
  row.className = `entry ${kind}`;
  const time = document.createElement("time");
  time.textContent = stamp();
  row.append(time, document.createTextNode(text));
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
}

function setTool(state, elapsed = 0) {
  toolActive = state === "Running" || state === "Waiting";
  $("tool-state").textContent = state;
  $("elapsed").textContent = elapsed;
  $("progress").style.width = `${Math.min(100, elapsed / 15 * 100)}%`;
}

function downsample(input, inputRate, outputRate = 24000) {
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const output = new Float32Array(Math.round(input.length / ratio));
  for (let i = 0; i < output.length; i++) {
    output[i] = input[Math.floor(i * ratio)];
  }
  return output;
}

function pcm16(float32) {
  const output = new ArrayBuffer(float32.length * 2);
  const view = new DataView(output);
  float32.forEach((sample, i) => {
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  });
  return output;
}

async function startAudio() {
  audioContext = new AudioContext();
  playbackTime = audioContext.currentTime;
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
  });
  source = audioContext.createMediaStreamSource(mediaStream);
  processor = audioContext.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (event) => {
    if (socket?.readyState !== WebSocket.OPEN) return;
    const samples = event.inputBuffer.getChannelData(0);
    socket.send(pcm16(downsample(samples, audioContext.sampleRate)));
  };
  source.connect(processor);
  processor.connect(audioContext.destination);
}

function playAudio(frame) {
  if (!audioContext || frame.byteLength <= 8) return;
  const header = new DataView(frame, 0, 8);
  const sampleRate = header.getUint32(0, true);
  const channels = header.getUint32(4, true);
  const pcm = new Int16Array(frame, 8);
  const buffer = audioContext.createBuffer(channels, pcm.length / channels, sampleRate);
  for (let channel = 0; channel < channels; channel++) {
    const data = buffer.getChannelData(channel);
    for (let i = 0; i < data.length; i++) data[i] = pcm[i * channels + channel] / 32768;
  }
  const node = audioContext.createBufferSource();
  node.buffer = buffer;
  node.connect(audioContext.destination);
  playbackTime = Math.max(playbackTime, audioContext.currentTime);
  node.start(playbackTime);
  playbackTime += buffer.duration;
}

function handleEvent(event) {
  const isDuringTool = toolActive;
  switch (event.type) {
    case "session_started":
      $("connection").textContent = "Connected";
      addEntry("timeline", "bot", `Session ready · ${event.voice} · ${event.model}`);
      break;
    case "transcription":
      addEntry("conversation", "user", `You: ${event.text}`);
      break;
    case "bot_text":
      if (event.final && event.text) {
        addEntry("conversation", isDuringTool ? "interim" : "bot", `Agent: ${event.text}`);
        addEntry("timeline", isDuringTool ? "interim" : "bot", `${isDuringTool ? "OUTPUT DURING TOOL" : "Assistant output"}: ${event.text}`);
      }
      break;
    case "tool_started":
      setTool("Running", 0);
      addEntry("timeline", "tool", `Voice Live requested the 15-second Agent Framework task.`);
      break;
    case "framework_tool_started":
      setTool("Waiting", 0);
      addEntry("timeline", "tool", "Agent Framework slow_operation started.");
      break;
    case "tool_waiting":
      setTool("Waiting", event.elapsed_seconds);
      addEntry("timeline", "tool", `Tool waiting · ${event.elapsed_seconds}s elapsed.`);
      break;
    case "tool_completed":
      setTool("Complete", 15);
      addEntry("timeline", "tool", "Tool completed.");
      break;
    case "tool_failed":
    case "error":
      setTool("Failed", 0);
      addEntry("timeline", "tool", `${event.type}: ${event.message}`);
      break;
    default:
      if (event.type === "user_speech_started" || event.type === "user_speech_stopped") {
        addEntry("timeline", "user", event.type.replaceAll("_", " "));
      }
  }
}

async function start() {
  $("start").disabled = true;
  $("connection").textContent = "Connecting…";
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.binaryType = "arraybuffer";
  socket.onopen = async () => {
    socket.send(JSON.stringify({ type: "start", interim_mode: $("mode").value }));
    await startAudio();
    $("stop").disabled = false;
  };
  socket.onmessage = async ({ data }) => {
    if (data instanceof ArrayBuffer) playAudio(data);
    else handleEvent(JSON.parse(data));
  };
  socket.onclose = () => stop(false);
}

function stop(closeSocket = true) {
  if (closeSocket) socket?.close();
  processor?.disconnect();
  source?.disconnect();
  mediaStream?.getTracks().forEach((track) => track.stop());
  audioContext?.close();
  $("connection").textContent = "Disconnected";
  $("start").disabled = false;
  $("stop").disabled = true;
}

$("start").addEventListener("click", () => start().catch((error) => {
  addEntry("timeline", "tool", `Start failed: ${error.message}`);
  stop();
}));
$("stop").addEventListener("click", () => stop());
