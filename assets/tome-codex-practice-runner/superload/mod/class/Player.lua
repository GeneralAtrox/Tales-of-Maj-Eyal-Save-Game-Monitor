local _M = loadPrevious(...)

local base_act = _M.act
local base_die = _M.die

local function open_result_file(result_path)
	if result_path == "" then return nil end

	local _, _, result_dir, result_name = result_path:find("^(.*)[/\\]([^/\\]+)$")
	if not result_dir or not result_name then
		print("[codex-practice] invalid result path:", result_path)
		return nil
	end

	local previous_write = fs.getWritePath()
	fs.setWritePath(result_dir)
	local file = fs.open(result_name, "w")
	if previous_write then fs.setWritePath(previous_write) end
	if not file then
		print("[codex-practice] failed to open result path:", result_path)
	end
	return file
end

local function finish_auto(status, winner, reason, detail)
	if not game or not game._codex_practice or game._codex_practice.finished then return end
	local practice = game._codex_practice
	practice.finished = true

	local result_path = (__module_extra_info and __module_extra_info.codex_practice_result_path) or ""
	local file = open_result_file(result_path)
	if file then
		local function esc(value)
			value = tostring(value or "")
			value = value:gsub("\\", "\\\\")
			value = value:gsub('"', '\\"')
			value = value:gsub("\r", "\\r")
			value = value:gsub("\n", "\\n")
			return value
		end
		file:write("{\n")
		file:write(('  "status": "%s",\n'):format(esc(status)))
		file:write(('  "winner": "%s",\n'):format(esc(winner)))
		file:write(('  "turns": %d,\n'):format(tonumber(practice.turns) or 0))
		file:write(('  "reason": "%s",\n'):format(esc(reason)))
		file:write(('  "detail": "%s"\n'):format(esc(detail)))
		file:write("}\n")
		file:close()
	end

	game:onTickEnd(function()
		core.game.exit_engine()
	end)
end

function _M:act()
	if not self._codex_practice_auto then
		return base_act(self)
	end

	local practice = game and game._codex_practice
	if not practice or not practice.scenario then
		return base_act(self)
	end

	practice.turns = (practice.turns or 0) + 1
	if practice.turns > (practice.scenario.turn_cap or 200) then
		finish_auto("Simulation timed out.", "timeout", "The fight hit the turn cap.", practice.scenario.template_label or "")
		return
	end

	while self:enoughEnergy() and not self.dead do
		if not mod.class.Actor.act(self) then return end
		local old_energy = self.energy.value
		self:doFOV()
		self:doAI()
		if not self.energy.used then
			self:waitTurn()
		end
		self:fireTalentCheck("callbackOnActEnd")
		if old_energy == self.energy.value then break end
	end
	game.paused = false
end

function _M:die(src, death_note)
	if self._codex_practice_auto then
		finish_auto("Simulation complete.", "enemy", death_note or "The player died.", (src and src.name) or "")
		return true
	end
	return base_die(self, src, death_note)
end

return _M
