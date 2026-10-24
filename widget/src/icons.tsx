// Lucide icons (MIT), fetched as SVG files rather than hand-drawn. Static, local markup.
import bookOpen from "./icons/book-open.svg?raw";
import chevronDown from "./icons/chevron-down.svg?raw";
import circleAlert from "./icons/circle-alert.svg?raw";
import externalLink from "./icons/external-link.svg?raw";
import headset from "./icons/headset.svg?raw";
import messageCircle from "./icons/message-circle.svg?raw";
import packageIcon from "./icons/package.svg?raw";
import rotateCcw from "./icons/rotate-ccw.svg?raw";
import sendHorizontal from "./icons/send-horizontal.svg?raw";
import shieldCheck from "./icons/shield-check.svg?raw";
import sparkles from "./icons/sparkles.svg?raw";
import trash from "./icons/trash-2.svg?raw";
import truck from "./icons/truck.svg?raw";
import undo from "./icons/undo-2.svg?raw";
import userRound from "./icons/user-round.svg?raw";
import x from "./icons/x.svg?raw";

const ICONS = {
  "book-open": bookOpen,
  "chevron-down": chevronDown,
  "circle-alert": circleAlert,
  "external-link": externalLink,
  headset,
  "message-circle": messageCircle,
  package: packageIcon,
  "rotate-ccw": rotateCcw,
  send: sendHorizontal,
  "shield-check": shieldCheck,
  sparkles,
  trash,
  truck,
  undo,
  "user-round": userRound,
  x,
};

export type IconName = keyof typeof ICONS;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <span
      className="sd-ico"
      style={{ width: size, height: size }}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: ICONS[name] }}
    />
  );
}
