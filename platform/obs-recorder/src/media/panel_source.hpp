// S5b/S6: native libobs status panel (right 480x1080 column).
//
// Layout contract: game capture is uniformly scaled into the left 1440x1080
// (4:3) region; this source paints the right 480x1080 column.  The bottom part
// of the panel tails the MCP overlay JSONL (thoughts/actions/results) so the
// recording itself shows the LLM reasoning, not only the game frame.
//
// Normal async video source: no OBS UI, no text plugin, no audio, no playback.
#pragma once

namespace a3 {

void RegisterStatusPanelSource();
void StopStatusPanelSourceThread();

} // namespace a3
