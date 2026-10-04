// A ScreenConnector for page tests: records where noVNC would connect instead of opening a WebSocket.
import type { ScreenConnector, ScreenSession } from "../components/TakeoverView";

export class FakeScreen implements ScreenSession {
  disconnects = 0;

  disconnect(): void {
    this.disconnects += 1;
  }
}

export class FakeConnector implements ScreenConnector {
  readonly connections: Array<{ target: HTMLElement; url: string }> = [];
  readonly screen = new FakeScreen();

  connect(target: HTMLElement, url: string): ScreenSession {
    this.connections.push({ target, url });
    return this.screen;
  }
}
