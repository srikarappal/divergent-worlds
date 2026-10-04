import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { episodeRecord, FakeApi } from "../test/fakeApi";
import { FakeConnector } from "../test/fakeScreens";
import { EpisodeLiveView } from "./EpisodeLiveView";
import { relayUrl, TakeoverControls, TakeoverView } from "./TakeoverView";

const RELAY_BASE = "wss://console.example/fleet/";

async function anOperatorTakesOverAndHandsBack(): Promise<void> {
  const api = new FakeApi();
  const connector = new FakeConnector();
  render(<TakeoverView api={api} runId="run_1" episodeId="episode_1" episodeRunning
                       screens={{ connector, relayBase: RELAY_BASE }} />);
  fireEvent.click(screen.getByRole("button", { name: "Take over" }));
  const operatorScreen = await screen.findByLabelText("operator screen");
  expect(api.callsTo("startTakeover")).toEqual([["run_1", "episode_1"]]);
  expect(connector.connections).toEqual([
    { target: operatorScreen, url: "wss://console.example/fleet/v1/takeover/session_1" }]);
  fireEvent.click(screen.getByRole("button", { name: "Hand back" }));
  expect(await screen.findByText(/Kept 2 actions as this episode's demonstration\./)).toBeInTheDocument();
  expect(connector.screen.disconnects).toBe(1);
  expect(api.callsTo("endTakeover")).toEqual([["run_1", "episode_1"]]);
  expect(screen.getByLabelText("end_takeover cli")).toHaveTextContent("rlworld episodes end-takeover run_1 episode_1");
}

test("an operator takes over, drives through noVNC and hands back", anOperatorTakesOverAndHandsBack);

function finishedEpisodesOfferNoTakeover(): void {
  const { container } = render(<TakeoverView api={new FakeApi()} runId="run_1" episodeId="episode_1"
                                             episodeRunning={false}
                                             screens={{ connector: new FakeConnector(), relayBase: RELAY_BASE }} />);
  expect(container).toBeEmptyDOMElement();
  expect(relayUrl("ws://localhost:5173/fleet", "/v1/takeover/x")).toBe("ws://localhost:5173/fleet/v1/takeover/x");
}

test("finished episodes offer no takeover", finishedEpisodesOfferNoTakeover);

async function theLiveViewShowsTheTakeoverPanelWhilePlaying(): Promise<void> {
  const api = new FakeApi();
  api.episodeUpdates = [episodeRecord("episode_1")];
  const controls = new TakeoverControls(api, "run_1", { connector: new FakeConnector(), relayBase: RELAY_BASE });
  render(<EpisodeLiveView api={api} runId="run_1" episodeId="episode_1" pollMilliseconds={60_000}
                          controls={controls} />);
  expect(await screen.findByRole("button", { name: "Take over" })).toBeInTheDocument();
  expect(screen.getByLabelText("takeover")).toHaveTextContent("Takeover");
}

test("the live view shows the takeover panel while the episode plays", theLiveViewShowsTheTakeoverPanelWhilePlaying);
