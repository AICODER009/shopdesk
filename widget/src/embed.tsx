import { createRoot, type Root } from "react-dom/client";
import { App, DEFAULT_SUGGESTIONS, type Suggestion, type WidgetOptions } from "./App";
import { ChatClient, tokenFromUrl, type TokenSource } from "./api";
import { installFonts } from "./fonts";
import { styles } from "./styles";

export interface ShopdeskConfig extends Partial<WidgetOptions> {
  apiBase: string;
  /** Storefront endpoint returning {"token": "<jwt>"} for a signed-in shopper, or null. */
  tokenUrl?: string;
  /** Alternative to tokenUrl when the shop already has the token in JS. */
  getToken?: TokenSource;
  accent?: string;
  /** "inherit" uses the shop's own font instead of the bundled Inter. */
  font?: "inter" | "inherit";
  /** Where shopdesk-widget.js and its fonts/ folder are served from. */
  assetBase?: string;
  suggestions?: Suggestion[];
}

const DEFAULTS: WidgetOptions = {
  title: "Customer support",
  greeting: "Hi! How can we help today?",
  privacyNote: "AI answers from our help centre · chats kept 30 days",
};

// Captured at load time; document.currentScript is null once the script has run.
const script = document.currentScript as HTMLScriptElement | null;
const scriptBase = script?.src ? script.src.slice(0, script.src.lastIndexOf("/") + 1) : "";

let root: Root | null = null;

export function init(config: ShopdeskConfig): void {
  if (root) return;
  const host = document.createElement("div");
  host.id = "shopdesk-widget";
  document.body.appendChild(host);

  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = styles;
  shadow.appendChild(style);
  if (config.accent) host.style.setProperty("--sd-accent", config.accent);
  if (config.font === "inherit") host.style.setProperty("--sd-font", "inherit");
  else installFonts(config.assetBase ?? scriptBase);

  const mount = document.createElement("div");
  shadow.appendChild(mount);

  const client = new ChatClient(
    config.apiBase.replace(/\/$/, ""),
    config.getToken ?? tokenFromUrl(config.tokenUrl),
  );
  const options: WidgetOptions = {
    title: config.title ?? DEFAULTS.title,
    greeting: config.greeting ?? DEFAULTS.greeting,
    privacyNote: config.privacyNote ?? DEFAULTS.privacyNote,
    suggestions: config.suggestions ?? DEFAULT_SUGGESTIONS,
  };
  root = createRoot(mount);
  root.render(<App client={client} options={options} />);
}

// <script src=".../shopdesk-widget.js" data-api-base="https://support-api.example.com"
//         data-token-url="/account/support-token" defer></script>
if (script?.dataset.apiBase) {
  const start = () =>
    init({
      apiBase: script.dataset.apiBase!,
      tokenUrl: script.dataset.tokenUrl,
      title: script.dataset.title,
      accent: script.dataset.accent,
      greeting: script.dataset.greeting,
      font: script.dataset.font === "inherit" ? "inherit" : "inter",
    });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
}
