import { FONT_FAMILY } from "./fonts";

// Injected into the widget's shadow root, so none of it leaks into (or is overridden by)
// the shop's stylesheet. Hosts theme it through --sd-accent (and optionally --sd-font).
export const styles = `
:host {
  --sd-accent: #1f4d3f;
  --sd-accent-hover: color-mix(in oklab, var(--sd-accent) 86%, black);
  --sd-accent-soft: color-mix(in oklab, var(--sd-accent) 10%, transparent);
  --sd-on-accent: #ffffff;

  --sd-bg: #ffffff;
  --sd-canvas: #f7f7f4;
  --sd-surface: #f0f0eb;
  --sd-line: #e4e3dc;
  --sd-text: #191b1a;
  --sd-muted: #646966;
  --sd-faint: #8c918e;
  --sd-danger: #b03a2e;
  --sd-ok: #2f9e62;

  --sd-font: "${FONT_FAMILY}", ui-sans-serif, "Segoe UI", Roboto, sans-serif;
  --sd-radius: 20px;
  --sd-ease: cubic-bezier(0.22, 1, 0.36, 1);
  --sd-shadow:
    0 1px 2px rgba(17, 24, 21, 0.06),
    0 8px 24px -6px rgba(17, 24, 21, 0.14),
    0 28px 64px -16px rgba(17, 24, 21, 0.28);

  all: initial;
  font-family: var(--sd-font);
  font-size: 14px;
  line-height: 1.5;
  color: var(--sd-text);
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "cv11", "ss01";
}

@media (prefers-color-scheme: dark) {
  :host {
    --sd-bg: #141615;
    --sd-canvas: #191c1b;
    --sd-surface: #232726;
    --sd-line: #2f3432;
    --sd-text: #eceeed;
    --sd-muted: #a4aaa7;
    --sd-faint: #7c8380;
    --sd-accent-soft: color-mix(in oklab, var(--sd-accent) 30%, transparent);
    --sd-shadow:
      0 1px 2px rgba(0, 0, 0, 0.4),
      0 28px 64px -16px rgba(0, 0, 0, 0.7);
  }
}

*, *::before, *::after { box-sizing: border-box; }
p { margin: 0; }
button { font: inherit; color: inherit; cursor: pointer; }
:focus-visible { outline: 2px solid var(--sd-accent); outline-offset: 2px; }

.sd-ico { display: inline-flex; flex: none; }
.sd-ico svg { width: 100%; height: 100%; }
.sd-ico g, .sd-ico path { stroke-width: 1.75; }

.sd-root { position: fixed; right: 24px; bottom: 24px; z-index: 2147483000; }

/* Launcher ------------------------------------------------------------------ */

.sd-launcher {
  position: relative; width: 60px; height: 60px; border: 0; border-radius: 50%;
  background: var(--sd-accent); color: var(--sd-on-accent);
  display: grid; place-items: center; box-shadow: var(--sd-shadow);
  transition: transform 200ms var(--sd-ease), background-color 150ms;
  float: right;
}
.sd-launcher:hover { background: var(--sd-accent-hover); transform: translateY(-2px); }
.sd-launcher:active { transform: scale(0.96); }
.sd-launcher-icon {
  grid-area: 1 / 1; display: grid; place-items: center;
  transition: opacity 180ms var(--sd-ease), transform 260ms var(--sd-ease);
}
.sd-launcher-close { opacity: 0; transform: rotate(-60deg) scale(0.6); }
.sd-launcher[data-open="true"] .sd-launcher-chat { opacity: 0; transform: rotate(60deg) scale(0.6); }
.sd-launcher[data-open="true"] .sd-launcher-close { opacity: 1; transform: none; }
.sd-badge {
  position: absolute; top: 3px; right: 3px; width: 14px; height: 14px; border-radius: 50%;
  background: #e5484d; border: 2.5px solid var(--sd-bg);
}

/* Panel --------------------------------------------------------------------- */

.sd-panel {
  position: absolute; right: 0; bottom: 76px;
  width: 400px; height: min(660px, calc(100vh - 120px));
  display: flex; flex-direction: column; overflow: hidden;
  background: var(--sd-canvas); border: 1px solid var(--sd-line);
  border-radius: var(--sd-radius); box-shadow: var(--sd-shadow);
  transform-origin: bottom right;
  animation: sd-panel-in 260ms var(--sd-ease);
}
@keyframes sd-panel-in {
  from { opacity: 0; transform: translateY(12px) scale(0.97); }
  to { opacity: 1; transform: none; }
}

.sd-header {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 10px 14px 16px; background: var(--sd-accent); color: var(--sd-on-accent);
}
.sd-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
.sd-identity > div { min-width: 0; }
.sd-avatar {
  width: 38px; height: 38px; border-radius: 12px; flex: none;
  display: grid; place-items: center;
  background: rgba(255, 255, 255, 0.14); box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.18);
}
.sd-header h2 {
  margin: 0; font-size: 15px; font-weight: 640; letter-spacing: -0.012em; line-height: 1.25;
}
.sd-presence {
  display: flex; align-items: center; gap: 6px; margin-top: 2px;
  font-size: 12px; line-height: 1.3; opacity: 0.82;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sd-dot {
  width: 7px; height: 7px; border-radius: 50%; background: #5ee39b; flex: none;
  box-shadow: 0 0 0 3px rgba(94, 227, 155, 0.22);
}
.sd-actions { display: flex; gap: 2px; flex: none; }
.sd-icon-btn {
  width: 36px; height: 36px; border: 0; border-radius: 10px; background: transparent;
  display: grid; place-items: center; color: inherit; opacity: 0.88;
  transition: background-color 150ms, opacity 150ms;
}
.sd-icon-btn:hover { background: rgba(255, 255, 255, 0.14); opacity: 1; }

/* Delete confirmation --------------------------------------------------------- */

.sd-confirm {
  display: flex; flex-direction: column; gap: 12px; padding: 14px 16px;
  background: var(--sd-bg); border-bottom: 1px solid var(--sd-line);
  animation: sd-fade-down 200ms var(--sd-ease);
}
.sd-confirm p { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.sd-confirm p span { color: var(--sd-muted); }
.sd-confirm div { display: flex; justify-content: flex-end; gap: 8px; }
.sd-btn {
  border: 0; border-radius: 10px; padding: 7px 14px; font-size: 13px; font-weight: 560;
  letter-spacing: 0.01em; transition: background-color 150ms;
}
.sd-btn-danger { background: var(--sd-danger); color: #fff; }
.sd-btn-danger:hover { background: color-mix(in oklab, var(--sd-danger) 86%, black); }
.sd-btn-ghost { background: transparent; color: var(--sd-text); box-shadow: inset 0 0 0 1px var(--sd-line); }
.sd-btn-ghost:hover { background: var(--sd-surface); }
@keyframes sd-fade-down { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }

/* Conversation ---------------------------------------------------------------- */

.sd-log {
  flex: 1; overflow-y: auto; overscroll-behavior: contain;
  padding: 20px 16px 12px; display: flex; flex-direction: column;
  scrollbar-width: thin; scrollbar-color: var(--sd-line) transparent;
}

.sd-welcome { padding: 4px 4px 16px; }
.sd-eyebrow {
  font-size: 11px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--sd-faint);
}
.sd-greeting {
  margin-top: 6px; font-size: 19px; font-weight: 600; line-height: 1.3;
  letter-spacing: -0.016em; color: var(--sd-text); text-wrap: balance;
}

.sd-suggestions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 0 0 8px; }
.sd-suggestion {
  display: flex; align-items: center; gap: 8px; padding: 11px 12px; text-align: left;
  border: 1px solid var(--sd-line); border-radius: 14px; background: var(--sd-bg);
  font-size: 13px; font-weight: 520; line-height: 1.3; color: var(--sd-text);
  transition: border-color 150ms, transform 200ms var(--sd-ease), box-shadow 200ms;
}
.sd-suggestion .sd-ico { color: var(--sd-accent); }
@media (prefers-color-scheme: dark) { .sd-suggestion .sd-ico { color: var(--sd-muted); } }
.sd-suggestion:hover {
  border-color: color-mix(in oklab, var(--sd-accent) 45%, var(--sd-line));
  transform: translateY(-1px); box-shadow: 0 4px 12px -6px rgba(17, 24, 21, 0.25);
}

.sd-row {
  display: flex; flex-direction: column; margin-top: 14px;
  animation: sd-rise 240ms var(--sd-ease);
}
.sd-row.sd-grouped { margin-top: 6px; }
.sd-row-customer { align-items: flex-end; }
.sd-row-assistant { align-items: flex-start; }
@keyframes sd-rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

.sd-bubble { max-width: 86%; padding: 10px 14px; border-radius: 18px; }
.sd-bubble p { white-space: pre-wrap; overflow-wrap: anywhere; }
.sd-bubble p a { color: inherit; text-decoration-thickness: 1px; text-underline-offset: 2px; }
.sd-bubble-assistant p a { color: var(--sd-accent); }
@media (prefers-color-scheme: dark) { .sd-bubble-assistant p a { color: inherit; } }
.sd-bubble-assistant {
  background: var(--sd-bg); border: 1px solid var(--sd-line); border-bottom-left-radius: 6px;
}
.sd-bubble-customer {
  background: var(--sd-accent); color: var(--sd-on-accent); border-bottom-right-radius: 6px;
}
.sd-time {
  margin: 4px 6px 0; font-size: 11px; color: var(--sd-faint);
  font-variant-numeric: tabular-nums;
}

.sd-sources {
  display: flex; flex-direction: column; gap: 4px; margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--sd-line);
}
.sd-sources-label {
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--sd-faint); margin-bottom: 2px;
}
.sd-sources a, .sd-source-plain {
  display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 10px;
  background: var(--sd-canvas); color: var(--sd-text); font-size: 13px; font-weight: 520;
  text-decoration: none; transition: background-color 150ms;
}
.sd-sources a > span:not(.sd-ico) { flex: 1; }
.sd-sources a .sd-ico:first-child, .sd-source-plain .sd-ico { color: var(--sd-accent); }
.sd-sources a .sd-ico:last-child { color: var(--sd-faint); }
.sd-sources a:hover { background: var(--sd-accent-soft); }
@media (prefers-color-scheme: dark) {
  .sd-sources a .sd-ico:first-child, .sd-source-plain .sd-ico { color: var(--sd-muted); }
}

.sd-handoff {
  display: flex; align-items: center; gap: 10px; margin-top: 8px; padding: 10px 14px 10px 10px;
  border-radius: 14px; background: var(--sd-accent-soft);
  box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--sd-accent) 22%, transparent);
}
.sd-handoff-icon {
  width: 32px; height: 32px; border-radius: 10px; display: grid; place-items: center;
  background: var(--sd-accent); color: var(--sd-on-accent); flex: none;
}
.sd-handoff div { display: flex; flex-direction: column; line-height: 1.3; }
.sd-handoff strong { font-size: 13px; font-weight: 600; }
.sd-handoff div span { font-size: 12px; color: var(--sd-muted); font-variant-numeric: tabular-nums; }

.sd-typing { display: flex; gap: 5px; padding: 14px 16px; }
.sd-typing span {
  width: 6px; height: 6px; border-radius: 50%; background: var(--sd-faint);
  animation: sd-bounce 1.2s infinite var(--sd-ease);
}
.sd-typing span:nth-child(2) { animation-delay: 0.15s; }
.sd-typing span:nth-child(3) { animation-delay: 0.3s; }
@keyframes sd-bounce {
  0%, 70%, 100% { transform: none; opacity: 0.45; }
  35% { transform: translateY(-4px); opacity: 1; }
}

.sd-notice {
  display: flex; align-items: center; gap: 6px; align-self: center; margin-top: 14px;
  padding: 6px 12px; border-radius: 999px; background: var(--sd-surface);
  color: var(--sd-muted); font-size: 12.5px;
}
.sd-notice .sd-ico { color: var(--sd-danger); }
.sd-retry {
  display: inline-flex; align-items: center; gap: 6px; align-self: center; margin-top: 8px;
  padding: 6px 14px; border-radius: 999px; border: 1px solid var(--sd-line);
  background: var(--sd-bg); font-size: 13px; font-weight: 540;
}
.sd-retry:hover { border-color: var(--sd-accent); }

/* Composer -------------------------------------------------------------------- */

.sd-composer {
  display: flex; align-items: flex-end; gap: 8px; padding: 10px 12px 8px;
  background: var(--sd-canvas);
}
.sd-field {
  position: relative; flex: 1; display: flex; border-radius: 16px; background: var(--sd-bg);
  border: 1px solid var(--sd-line); transition: border-color 150ms, box-shadow 150ms;
}
.sd-field:focus-within {
  border-color: color-mix(in oklab, var(--sd-accent) 55%, var(--sd-line));
  box-shadow: 0 0 0 4px var(--sd-accent-soft);
}
.sd-field textarea {
  flex: 1; resize: none; border: 0; outline: 0; background: transparent;
  padding: 11px 14px; max-height: 132px; font: inherit; color: var(--sd-text);
  line-height: 1.45;
}
.sd-field textarea::placeholder { color: var(--sd-faint); }
.sd-counter {
  position: absolute; right: 10px; bottom: -18px; font-size: 11px; color: var(--sd-muted);
  font-variant-numeric: tabular-nums;
}
.sd-send {
  width: 44px; height: 44px; flex: none; border: 0; border-radius: 14px;
  background: var(--sd-accent); color: var(--sd-on-accent); display: grid; place-items: center;
  transition: background-color 150ms, transform 200ms var(--sd-ease), opacity 150ms;
}
.sd-send:hover:not(:disabled) { background: var(--sd-accent-hover); }
.sd-send:active:not(:disabled) { transform: scale(0.94); }
.sd-send:disabled { opacity: 0.35; cursor: default; }

.sd-note {
  display: flex; align-items: center; justify-content: center; gap: 5px;
  padding: 0 16px 12px; font-size: 11.5px; color: var(--sd-faint); text-align: center;
}

/* Small screens & motion ------------------------------------------------------ */

@media (max-width: 480px) {
  .sd-root { right: 16px; bottom: 16px; }
  .sd-panel {
    position: fixed; inset: 0; width: 100vw; height: 100dvh; border: 0; border-radius: 0;
    padding-bottom: env(safe-area-inset-bottom);
  }
  .sd-panel ~ .sd-launcher { display: none; }
  .sd-suggestions { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
`;
