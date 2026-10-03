// Types for the part of noVNC's RFB client the console uses; the package ships none.
declare module "@novnc/novnc" {
  export interface RfbOptions {
    shared?: boolean;
    wsProtocols?: string[];
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: RfbOptions);
    scaleViewport: boolean;
    disconnect(): void;
  }
}
