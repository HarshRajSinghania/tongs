// Output framing from the desktop service: newline-delimited frames arrive in
// arbitrary pipe chunks and must be reassembled exactly, in time proportional
// to their size, with the response frame limit applied at the same byte count.
import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate as nextTurn } from "node:timers/promises";
import { MAX_RESPONSE_BYTES } from "../../../desktop/dist/src/main/sidecar.js";
import { harness, transportFor } from "./fake-sidecar.mjs";

const PIPE_CHUNK = 64 * 1024;

function watchCrashes(transport) {
  const crashes = [];
  transport.on("crash", (error) => crashes.push(error.code));
  return crashes;
}

function writeInChunks(stream, bytes, size) {
  for (let offset = 0; offset < bytes.length; offset += size) {
    stream.write(bytes.subarray(offset, offset + size));
  }
}

function responseFrame(id, result) {
  return `${JSON.stringify({ v: 1, type: "response", id, result })}\n`;
}

// A response whose frame, without its newline, is exactly `bytes` long. The
// filler mixes in two-byte characters so chunk edges can split one.
function responseOfSize(id, bytes) {
  const empty = Buffer.byteLength(JSON.stringify({ v: 1, type: "response", id, result: "" }));
  let room = bytes - empty;
  const pairs = Math.floor(room / 5);
  let filler = "abé".repeat(pairs);
  room -= pairs * 4;
  filler += "a".repeat(room);
  const frame = Buffer.from(responseFrame(id, filler));
  assert.equal(frame.length, bytes + 1);
  return { frame, filler };
}

test("an 8 MiB response split into 64 KiB pipe chunks parses intact", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const read = transport.requestRead("reviews.list", {});
  const { frame, filler } = responseOfSize(read.requestId, MAX_RESPONSE_BYTES);
  writeInChunks(fake.children[0].stdout, frame, PIPE_CHUNK);
  const result = await read.result;
  // Compare as booleans and lengths: a failing 8 MiB string diff would be huge.
  assert.equal(typeof result, "string");
  assert.equal(result.length, filler.length);
  assert.equal(result === filler, true);
  assert.deepEqual(crashes, []);
  await transport.stop();
});

test("several frames in one chunk all parse, and a trailing partial waits", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const events = [];
  transport.on("event", (event) => events.push(event.name));
  const reads = [
    transport.requestRead("reviews.list", {}),
    transport.requestRead("commits.list", { review: "review-handle" }),
    transport.requestRead("pipelines.list", {}),
    transport.requestRead("assets.list", {}),
  ];
  const tail = Buffer.from(responseFrame(reads[3].requestId, { n: 3 }));
  const chunk = Buffer.concat([
    Buffer.from(responseFrame(reads[0].requestId, { n: 0 })),
    Buffer.from(`${JSON.stringify({ v: 1, type: "event", sequence: 1, event: "tick", data: {} })}\n`),
    Buffer.from(responseFrame(reads[1].requestId, { n: 1 })),
    Buffer.from(responseFrame(reads[2].requestId, { n: 2 })),
    tail.subarray(0, 10),
  ]);
  fake.children[0].stdout.write(chunk);
  assert.deepEqual(await reads[0].result, { n: 0 });
  assert.deepEqual(await reads[1].result, { n: 1 });
  assert.deepEqual(await reads[2].result, { n: 2 });
  assert.deepEqual(events, ["tick"]);
  let settled = false;
  reads[3].result.then(() => (settled = true), () => (settled = true));
  await nextTurn();
  assert.equal(settled, false);
  fake.children[0].stdout.write(tail.subarray(10));
  assert.deepEqual(await reads[3].result, { n: 3 });
  assert.deepEqual(crashes, []);
  await transport.stop();
});

test("a frame split across many chunks, mid-character and before its newline, parses", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const first = transport.requestRead("reviews.list", {});
  const second = transport.requestRead("commits.list", { review: "review-handle" });
  const text = "café ☃ 😀 done";
  writeInChunks(fake.children[0].stdout, Buffer.from(responseFrame(first.requestId, text)), 1);
  assert.equal(await first.result, text);
  // The newline arrives alone, after the whole frame body.
  const body = Buffer.from(responseFrame(second.requestId, ["x", "y"]));
  writeInChunks(fake.children[0].stdout, body.subarray(0, body.length - 1), 7);
  fake.children[0].stdout.write("\n");
  assert.deepEqual(await second.result, ["x", "y"]);
  assert.deepEqual(crashes, []);
  await transport.stop();
});

test("unterminated output at the limit waits, one byte more trips frame_too_large", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const pending = transport.requestRead("reviews.list", {});
  let settled = false;
  pending.result.then(() => (settled = true), () => (settled = true));
  writeInChunks(fake.children[0].stdout, Buffer.alloc(MAX_RESPONSE_BYTES, 0x20), PIPE_CHUNK);
  await nextTurn();
  assert.equal(settled, false);
  assert.deepEqual(crashes, []);
  fake.children[0].stdout.write(Buffer.from(" "));
  await assert.rejects(pending.result, { code: "frame_too_large" });
  assert.deepEqual(crashes, ["frame_too_large"]);
});

test("a terminated frame one byte over the limit is an invalid frame", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const pending = transport.requestRead("reviews.list", {});
  writeInChunks(fake.children[0].stdout, Buffer.alloc(MAX_RESPONSE_BYTES, 0x20), PIPE_CHUNK);
  fake.children[0].stdout.write(Buffer.from(" \n"));
  await assert.rejects(pending.result, { code: "invalid_frame" });
  assert.deepEqual(crashes, ["invalid_frame"]);
});
