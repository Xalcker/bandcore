"use strict";
// node player_render.js <carpeta> <salida.wav>
// La carpeta trae lo que recibe la página del editor: data.json (el payload), player.json (/api/player) y player.js
// (la emulación del chip más el adaptador que define window.BandPlayer). Escribe la mezcla como WAV mono de 16 bits,
// normalizada a -1 dB (sin subir más de 4 veces).
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [dir, wav] = process.argv.slice(2);
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(path.join(dir, "player.js"), "utf8"), { filename: "player.js" });
const DATA = JSON.parse(fs.readFileSync(path.join(dir, "data.json"), "utf8"));
const player = JSON.parse(fs.readFileSync(path.join(dir, "player.json"), "utf8"));

(async () => {
  const result = await window.BandPlayer.render(player, DATA, {
    sampleRate: 44100, onProgress() {}, yieldFn: () => Promise.resolve(),
  });
  const audio = result.audio;
  const rate = result.sampleRate || 44100;
  let peak = 0;
  for (let i = 0; i < audio.length; i++) peak = Math.max(peak, Math.abs(audio[i]));
  const gain = peak > 0 ? Math.min(0.9 / peak, 4) : 1;
  const n = audio.length;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write("WAVEfmt ", 8); buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28);
  buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write("data", 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) {
    buf.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(audio[i] * gain * 32767))), 44 + i * 2);
  }
  fs.writeFileSync(wav, buf);
  console.log(`${wav}: ${(n / rate).toFixed(1)} s, pico original ${peak.toFixed(3)}`);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
