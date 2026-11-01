export const AvatarState = {
  IDLE: "idle",
  WALK: "walk",
  TYPING: "typing",
  READING: "reading",
  WAITING: "waiting",
  SUCCESS: "success",
  ERROR: "error",
};

const STATE_ORDER = [
  AvatarState.IDLE,
  AvatarState.WALK,
  AvatarState.TYPING,
  AvatarState.READING,
  AvatarState.WAITING,
  AvatarState.SUCCESS,
  AvatarState.ERROR,
];

export const MOTION_TIMING = {
  frameMs: 150,
  successHoldMs: 1100,
  errorHoldMs: 1400,
};

export function stateRowIndex(state) {
  const idx = STATE_ORDER.indexOf(String(state || AvatarState.IDLE));
  return idx < 0 ? 0 : idx;
}

export function mapToolEventToState(evt) {
  const type = String(evt?.type || "");
  const tool = String(evt?.tool || "").toLowerCase();
  const status = String(evt?.status || "").toLowerCase();

  if (type === "approval_wait") return AvatarState.WAITING;
  if (type === "tool_start") {
    if (["fs_read", "fs_search", "glob", "web_fetch", "web_search"].includes(tool)) {
      return AvatarState.READING;
    }
    return AvatarState.TYPING;
  }
  if (type === "tool_result") {
    return status === "succeeded" ? AvatarState.SUCCESS : AvatarState.ERROR;
  }
  if (type === "finalize") return AvatarState.IDLE;
  return AvatarState.IDLE;
}

export class AvatarEngine {
  constructor({ onTransition } = {}) {
    this.state = AvatarState.IDLE;
    this.frame = 0;
    this.lastTick = 0;
    this.stateChangedAt = Date.now();
    this.onTransition = typeof onTransition === "function" ? onTransition : null;
  }

  setState(next) {
    const token = String(next || AvatarState.IDLE);
    if (token === this.state) return;
    const prev = this.state;
    this.state = token;
    this.frame = 0;
    this.stateChangedAt = Date.now();
    if (this.onTransition) {
      this.onTransition({ from: prev, to: this.state });
    }
  }

  maybeAutoReturnIdle(now = Date.now()) {
    if (this.state !== AvatarState.SUCCESS && this.state !== AvatarState.ERROR) return;
    const hold = this.state === AvatarState.SUCCESS ? MOTION_TIMING.successHoldMs : MOTION_TIMING.errorHoldMs;
    if (now - this.stateChangedAt >= hold) {
      this.setState(AvatarState.IDLE);
    }
  }

  tick(now = Date.now(), frameCount = 7) {
    if (!this.lastTick) this.lastTick = now;
    if (now - this.lastTick >= MOTION_TIMING.frameMs) {
      this.frame = (this.frame + 1) % Math.max(1, Number(frameCount || 7));
      this.lastTick = now;
    }
    this.maybeAutoReturnIdle(now);
    return { state: this.state, frame: this.frame, row: stateRowIndex(this.state) };
  }
}
