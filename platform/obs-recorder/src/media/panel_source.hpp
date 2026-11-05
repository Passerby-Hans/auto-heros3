// S5b: native libobs status panel (right 480x1080 column).
//
// The game capture is scaled uniformly into a 1440x1080 (4:3) region; this
// source paints the remaining 480x1080 column.  It is a normal async video
// source: no OBS UI, no text plugin, no audio, no playback.  GDI is used only
// to rasterise the panel into a BGRA frame handed to libobs.
#pragma once

namespace a3 {

void RegisterStatusPanelSource();
void StopStatusPanelSourceThread();

} // namespace a3
