// The real screen connector: noVNC's RFB client over fleetd's takeover relay. Only the browser entry imports it.
import RFB from "@novnc/novnc";

import type { ScreenConnector, ScreenSession } from "./TakeoverView";

export class NoVncConnector implements ScreenConnector {
  connect(target: HTMLElement, url: string): ScreenSession {
    const rfb = new RFB(target, url, { shared: true, wsProtocols: ["binary"] });
    rfb.scaleViewport = true;
    return rfb;
  }
}

// fleetd's relay as the browser reaches it: the console's own origin under /fleet, which the gateway routes.
export function fleetRelayBase(location: Location): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/fleet`;
}
