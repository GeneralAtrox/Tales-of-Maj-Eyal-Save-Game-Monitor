local function json_escape(value)
	value = tostring(value or "")
	value = value:gsub("\\", "\\\\")
	value = value:gsub('"', '\\"')
	value = value:gsub("\r", "\\r")
	value = value:gsub("\n", "\\n")
	return value
end

local function write_result(status, winner, turns, reason, detail)
	local result_path = __module_extra_info and __module_extra_info.codex_practice_result_path or ""
	if result_path == "" then return end

	local _, _, result_dir, result_name = result_path:find("^(.*)[/\\]([^/\\]+)$")
	if not result_dir or not result_name then
		print("[codex-practice-boot] invalid result path:", result_path)
		return
	end

	local previous_write = fs.getWritePath()
	fs.setWritePath(result_dir)
	local file = fs.open(result_name, "w")
	if previous_write then fs.setWritePath(previous_write) end
	if not file then
		print("[codex-practice-boot] failed to open result path:", result_path)
		return
	end
	file:write("{\n")
	file:write(('  "status": "%s",\n'):format(json_escape(status)))
	file:write(('  "winner": "%s",\n'):format(json_escape(winner)))
	file:write(('  "turns": %d,\n'):format(tonumber(turns) or 0))
	file:write(('  "reason": "%s",\n'):format(json_escape(reason)))
	file:write(('  "detail": "%s"\n'):format(json_escape(detail)))
	file:write("}\n")
	file:close()
end

local function fail_boot(reason, detail)
	write_result("Simulation failed before the save was loaded.", "error", 0, reason or "", detail or "")
end

local function launch_practice_save()
	if game._codex_practice_boot_started then return end
	game._codex_practice_boot_started = true

	local target_module = __module_extra_info and __module_extra_info.codex_boot_module or "tome"
	local target_save = __module_extra_info and __module_extra_info.codex_boot_save_name or ""
	local forward_info = __module_extra_info and __module_extra_info.codex_boot_forward_info or nil
	if target_save == "" then
		fail_boot("No target save name was supplied.", "")
		return
	end

	local Module = require "engine.Module"
	local modules = Module:listSavefiles()
	local summary
	for _, entry in ipairs(modules) do
		if entry.short_name == target_module then
			summary = entry
			break
		end
	end
	if not summary then
		fail_boot("Practice module is not available.", target_module)
		return
	end

	local selected
	local selected_mod
	for _, save in ipairs(summary.savefiles or {}) do
		if save.short_name == target_save then
			selected = save
			local mod_string = ("%s-%d.%d.%d"):format(
				summary.short_name,
				save.module_version and save.module_version[1] or -1,
				save.module_version and save.module_version[2] or -1,
				save.module_version and save.module_version[3] or -1
			)
			selected_mod = modules[mod_string]
			if not selected_mod and save.module_version and summary.versions and summary.versions[1]
				and summary.versions[1].version and engine.version_patch_same(summary.versions[1].version, save.module_version) then
				selected_mod = summary.versions[1]
			end
			if not selected_mod and summary.versions and summary.versions[1] then
				selected_mod = summary.versions[1]
			end
			break
		end
	end
	if not selected then
		fail_boot("Practice save was not found.", target_save)
		return
	end
	if not selected_mod then
		fail_boot("No compatible module version was found for the practice save.", target_save)
		return
	end

	selected.mod = selected_mod
	selected.base_name = selected.base_name or selected.short_name
	-- Reboot into the target module instead of instantiating it inside the boot
	-- Lua state; the clean restart keeps module globals/talent tables aligned with
	-- stock ToME's normal save-load path.
	Module:instanciate(selected.mod, selected.base_name, false, false, forward_info)
end

class:bindHook("Boot:runEnd", function(self, data)
	if not __module_extra_info or not __module_extra_info.codex_boot_save_name then return end
	if game._codex_practice_boot_queued then return end

	game._codex_practice_boot_queued = true
	game:onTickEnd(function()
		launch_practice_save()
	end)
end)
