#include <obs-module.h>
OBS_DECLARE_MODULE()
extern struct obs_output_info mp4_output_info;
extern struct obs_output_info mov_output_info;
const char *obs_module_text(const char *v){return v;}
bool obs_module_load(void){obs_register_output(&mp4_output_info);obs_register_output(&mov_output_info);return true;}
void obs_module_unload(void){}
