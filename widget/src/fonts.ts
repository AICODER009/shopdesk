// Inter is served from the same host as the widget, never from Google's CDN: embedding
// Google Fonts without consent has been ruled a GDPR breach in Germany (LG München, 2022).
// @font-face doesn't apply from inside a shadow root in every browser, so it goes in the
// document head under a family name the shop's own CSS won't collide with.
export const FONT_FAMILY = "Shopdesk Inter";

const FACES: Array<[file: string, range: string]> = [
  [
    "inter-latin-wght-normal.woff2",
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308," +
      "U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD",
  ],
  [
    "inter-latin-ext-wght-normal.woff2",
    "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329," +
      "U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113," +
      "U+2C60-2C7F,U+A720-A7FF",
  ],
];

export function installFonts(assetBase: string): void {
  if (document.getElementById("shopdesk-fonts")) return;
  const style = document.createElement("style");
  style.id = "shopdesk-fonts";
  style.textContent = FACES.map(
    ([file, range]) => `@font-face {
  font-family: "${FONT_FAMILY}";
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: url("${assetBase}fonts/${file}") format("woff2-variations");
  unicode-range: ${range};
}`,
  ).join("\n");
  document.head.appendChild(style);
}
