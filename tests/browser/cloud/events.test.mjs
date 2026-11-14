// Task 11 node-level tests: durable meeting events and the processing seam.
//
// The EventSource here is synthetic: it exercises URL construction, named-event
// parsing, cursor handling and honest processing updates. It makes no claim
// about a real browser EventSource, ASR, or live transcript quality.

import assert from "node:assert/strict";
import test from "node:test";

import { RecordingSession } from "../../../web/cloud/recording.mjs";
import {
  MEETING_EVENT_NAMES,
  meetingEventsUrl,
  openMeetingEvents,
} from "../../../web/cloud/session.mjs";

const MEETING = "meeting/a b";
const USER = "user-a";
const UPLOAD = "upload-1";

class FakeEventSource {
  constructor(url, init) {
    this.url = url;
    this.init = init;
    this.listeners = new Map();
    this.closed = 0;
    this.onerror = null;
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  emit(name, data, lastEventId = "1") {
    const listener = this.listeners.get(name);
    assert.ok(listener, `missing listener for ${name}`);
    listener({ data: JSON.stringify(data), lastEventId });
  }

  error(value = { code: "network" }) {
    this.onerror?.(value);
  }

  close() {
    this.closed += 1;
  }
}

function streamHarness(overrides = {}) {
  const seen = [];
  const errors = [];
  let source = null;
  const stream = openMeetingEvents({
    meetingId: MEETING,
    after: 7,
    baseUrl: "https://cloud.example",
    onEvent: (event) => seen.push(event),
    onError: (error) => errors.push(error),
    eventSourceFactory: (url, init) => {
      source = new FakeEventSource(url, init);
      return source;
    },
    ...overrides,
  });
  return { stream, seen, errors, source };
}

function fakeBuffer() {
  return {
    async put() {},
    async stats() {
      return { chunks: 0, bytes: 0, bufferedMs: 0, byTrack: {} };
    },
    async getWarning() {
      return null;
    },
    async putWarning() {},
    async clearWarning() {},
    async clearUpload() {},
  };
}

function recording() {
  return new RecordingSession({
    userId: USER,
    uploadId: UPLOAD,
    epoch: { id: "epoch-1", offsetMs: 0, anchorMs: 0 },
    buffer: fakeBuffer(),
    pump: { async flush() { return { ok: true, acked: 0 }; } },
    clock: { now: () => 1000 },
  });
}

function closeRecorder() {
  return {
    calls: 0,
    close() {
      this.calls += 1;
    },
  };
}

test("meeting_events_url_uses_encoded_meeting_and_initial_cursor_only", () => {
  const url = meetingEventsUrl(MEETING, { after: 7, baseUrl: "https://cloud.example" });
  assert.equal(url.origin, "https://cloud.example");
  assert.equal(url.pathname, "/api/cloud/meetings/meeting%2Fa%20b/events/stream");
  assert.equal(url.searchParams.get("after"), "7");
  assert.equal(url.searchParams.has("token"), false);
  assert.equal(url.searchParams.has("email"), false);

  const without = meetingEventsUrl(MEETING, { after: -1, baseUrl: "https://cloud.example" });
  assert.equal(without.searchParams.has("after"), false);
  assert.throws(() => meetingEventsUrl(""), /meetingId/);
});

test("opens_named_events_with_credentials_and_closes_once", () => {
  const { stream, seen, source } = streamHarness();
  assert.equal(source.init.withCredentials, true);
  assert.deepEqual([...source.listeners.keys()], [...MEETING_EVENT_NAMES]);
  const url = new URL(source.url);
  assert.equal(url.searchParams.get("after"), "7");
  assert.equal(url.href.includes("token"), false);

  source.emit("snapshot", { meeting: { id: "meeting-1" }, tasks: [] }, "12");
  assert.deepEqual(seen[0], {
    event: "snapshot",
    cursor: 12,
    data: { meeting: { id: "meeting-1" }, tasks: [] },
  });

  stream.close();
  stream.close();
  assert.equal(source.closed, 1, "close is idempotent");
  source.emit("task.updated", { task_id: "t1", status: "running" }, "13");
  assert.equal(seen.length, 1, "no event is delivered after close");
  source.error();
  assert.equal(seen.length, 1);
});

test("malformed_event_data_is_reported_and_not_dispatched", () => {
  const { seen, errors, source } = streamHarness();
  source.listeners.get("snapshot")({ data: "{not json", lastEventId: "1" });
  assert.equal(seen.length, 0);
  assert.equal(errors[0].code, "invalid_event_data");
  source.error();
  assert.deepEqual(errors[1], { code: "network" });
});

test("recording_applies_snapshot_tasks_and_transcript_without_inventing_state", () => {
  const session = recording();
  const before = JSON.parse(JSON.stringify({ capture: session.capture, upload: session.upload }));

  session.applyServerEvent({
    event: "snapshot",
    cursor: 3,
    data: {
      tasks: [
        { id: "task-2", status: "waiting", input_version: 2, generation: 1 },
        { id: "task-1", status: "completed", input_version: 1, generation: 0 },
      ],
      transcript: null,
    },
  });
  assert.equal(session.processing.state, "queued");
  assert.equal(session.processing.taskId, "task-2");

  session.applyServerEvent({
    event: "snapshot",
    cursor: 4,
    data: {
      tasks: [{ id: "task-2", status: "running", input_version: 2, generation: 1 }],
      transcript: null,
    },
  });
  assert.equal(session.processing.state, "running");

  session.applyServerEvent({
    event: "snapshot",
    cursor: 5,
    data: {
      tasks: [{ id: "task-2", status: "completed", input_version: 2, generation: 1 }],
      transcript: { id: "version-1", status: "ready", empty: false, warnings: [] },
    },
  });
  assert.deepEqual(session.processing, { state: "completed", transcriptId: "version-1" });

  session.applyServerEvent({
    event: "task.updated",
    cursor: 6,
    data: { task_id: "task-3", status: "failed", error_code: "probe_failed" },
  });
  assert.deepEqual(session.processing, {
    state: "failed",
    taskId: "task-3",
    errorCode: "probe_failed",
  });

  session.applyServerEvent({
    event: "transcript.published",
    cursor: 7,
    data: { version_id: "version-2", task_id: "task-3", warnings: [] },
  });
  assert.deepEqual(session.processing, {
    state: "completed",
    transcriptId: "version-2",
    taskId: "task-3",
  });

  const serialized = JSON.stringify(session.processing);
  for (const forbidden of ["text", "speaker", "payload", "audio"]) {
    assert.equal(serialized.includes(forbidden), false);
  }
  assert.deepEqual(
    JSON.parse(JSON.stringify({ capture: session.capture, upload: session.upload })),
    before,
    "server events never claim capture or upload truth",
  );

  // A delayed replay must not roll state backwards.
  session.applyServerEvent({
    event: "task.updated",
    cursor: 2,
    data: { task_id: "task-old", status: "running" },
  });
  assert.equal(session.processing.state, "completed");
});

test("recording_closes_attached_stream_on_expiry_and_logout_once", async () => {
  const session = recording();
  const stream = closeRecorder();
  const hook = closeRecorder();
  session.closeEvents = hook;
  assert.equal(session.attachEventStream(stream), stream);

  await session.handleSessionExpired();
  assert.equal(stream.calls, 1);
  assert.equal(hook.calls, 1);
  session.closeEventStream();
  assert.equal(stream.calls, 1, "closeEventStream stays idempotent");

  const second = recording();
  const secondStream = closeRecorder();
  second.attachEventStream(secondStream);
  await second.logout({ abandon: true });
  assert.equal(secondStream.calls, 1);
  assert.equal(second.attachEventStream(closeRecorder()), null, "late streams close immediately");
});
