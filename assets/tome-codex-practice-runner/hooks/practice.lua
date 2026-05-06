local function load_scenario()
	if not __module_extra_info or not __module_extra_info.codex_practice_scenario_path then return nil end
	if game and game._codex_practice and game._codex_practice.scenario then
		return game._codex_practice.scenario
	end

	local scenario_path = __module_extra_info.codex_practice_scenario_path
	local chunk, err = loadfile(scenario_path)
	if not chunk then
		print("[codex-practice] could not load scenario:", err)
		return nil
	end
	local ok, scenario = pcall(chunk)
	if not ok or type(scenario) ~= "table" then
		print("[codex-practice] scenario execution failed:", scenario)
		return nil
	end
	return scenario
end

local function json_escape(value)
	value = tostring(value or "")
	value = value:gsub("\\", "\\\\")
	value = value:gsub('"', '\\"')
	value = value:gsub("\r", "\\r")
	value = value:gsub("\n", "\\n")
	return value
end

local function actor_name(actor)
	if not actor then return "" end
	if actor.getName then
		local ok, name = pcall(function() return actor:getName() end)
		if ok and name then return tostring(name) end
	end
	return tostring(actor.name or "")
end

local function actor_role(actor)
	if not actor then return "" end
	if game and actor == game.player then return "player" end
	local practice = game and game._codex_practice
	if practice and practice.hostiles then
		for _, hostile in ipairs(practice.hostiles) do
			if actor == hostile then return "enemy" end
		end
	end
	return ""
end

local function clean_damage_message(message)
	message = tostring(message or "")
	message = message:gsub("#[^#]+#", "")
	message = message:gsub("%s+", " ")
	return message
end

local function record_damage_event(src, target, dam, message)
	local practice = game and game._codex_practice
	if not practice or practice.finished then return end
	local amount = tonumber(dam) or 0
	if amount <= 0 then return end
	practice.damage_events = practice.damage_events or {}
	if #practice.damage_events >= 80 then return end
	practice.damage_events[#practice.damage_events + 1] = {
		turn = tonumber(practice.turns) or 0,
		source = actor_name(src),
		source_role = actor_role(src),
		target = actor_name(target),
		target_role = actor_role(target),
		amount = amount,
		message = clean_damage_message(message),
	}
end

local function install_damage_trace()
	if not game or game._codex_practice_damage_trace_installed then return end
	if type(game.delayedLogDamage) ~= "function" then return end
	local base_delayed_log_damage = game.delayedLogDamage
	game._codex_practice_damage_trace_installed = true
	game.delayedLogDamage = function(self, src, target, dam, message, ...)
		record_damage_event(src, target, dam, message)
		return base_delayed_log_damage(self, src, target, dam, message, ...)
	end
end

local function write_damage_events(file, events)
	file:write('  "damage_events": [\n')
	for index, event in ipairs(events or {}) do
		local suffix = index < #(events or {}) and "," or ""
		file:write(
			(
				'    {"turn": %d, "source": "%s", "source_role": "%s", ' ..
				'"target": "%s", "target_role": "%s", "amount": %.3f, "message": "%s"}%s\n'
			):format(
				tonumber(event.turn) or 0,
				json_escape(event.source),
				json_escape(event.source_role),
				json_escape(event.target),
				json_escape(event.target_role),
				tonumber(event.amount) or 0,
				json_escape(event.message),
				suffix
			)
		)
	end
	file:write("  ]\n")
end

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

local function write_result(status, winner, turns, reason, detail)
	local result_path = (__module_extra_info and __module_extra_info.codex_practice_result_path) or ""
	if result_path == "" then
		local scenario = load_scenario()
		result_path = scenario and scenario.result_path or ""
	end
	local file = open_result_file(result_path)
	if not file then return end
	file:write("{\n")
	file:write(('  "status": "%s",\n'):format(json_escape(status)))
	file:write(('  "winner": "%s",\n'):format(json_escape(winner)))
	file:write(('  "turns": %d,\n'):format(tonumber(turns) or 0))
	file:write(('  "reason": "%s",\n'):format(json_escape(reason)))
	file:write(('  "detail": "%s",\n'):format(json_escape(detail)))
	write_damage_events(file, game and game._codex_practice and game._codex_practice.damage_events)
	file:write("}\n")
	file:close()
end

local function finish_practice(status, winner, reason, detail)
	if not game or not game._codex_practice then return end
	local practice = game._codex_practice
	if practice.finished then return end
	practice.finished = true

	write_result(status, winner, practice.turns or 0, reason or "", detail or "")

	if practice.scenario and practice.scenario.mode == "auto" then
		game:onTickEnd(function()
			core.game.exit_engine()
		end)
	end
end

local function find_actor_definition(definitions, ref)
	if ref.define_as and ref.define_as ~= "" and definitions[ref.define_as] then
		return definitions[ref.define_as]
	end

	local wanted = (ref.name or ""):lower()
	for _, definition in pairs(definitions) do
		if definition and definition.name and definition.name:lower() == wanted then
			return definition
		end
	end
	return nil
end

local function practice_enemy_die(self, src)
	local died = mod.class.NPC.die(self, src)
	if game and game._codex_practice and game._codex_practice.scenario
		and game._codex_practice.scenario.mode == "auto" then
		local living = 0
		for _, hostile in ipairs(game._codex_practice.hostiles or {}) do
			if hostile and not hostile.dead then living = living + 1 end
		end
		if living == 0 then
			finish_practice("Simulation complete.", "player", "All configured enemies were defeated.", self.name or "")
		end
	end
	return died
end

local function spawn_practice_enemy(ref)
	local NPC = require "mod.class.NPC"
	local definitions = NPC:loadList(ref.source_path)
	local definition = find_actor_definition(definitions, ref)
	if not definition then
		print("[codex-practice] could not find actor definition in:", ref.source_path, ref.name or "", ref.define_as or "")
		return nil
	end

	local actor = definition:clone()
	actor.forceLevelup = true
	actor.faction = "enemies"
	actor.energy.value = 0
	actor:resolve()
	actor:resolve(nil, true)
	actor:resetToFull()
	actor.on_die = practice_enemy_die
	game.zone:addEntity(game.level, actor, "actor", ref.x, ref.y)
	actor:setTarget(game.player)
	return actor
end

local function enter_practice_arena(zone, level)
	local practice = game and game._codex_practice
	if not practice or practice.arena_loaded then return end
	practice.arena_loaded = true

	local scenario = practice.scenario
	local player_spot = scenario.player or {}
	game.player:move(player_spot.x or 4, player_spot.y or 8, true)
	game.player.energy.value = game.energy_to_act
	game.player.no_resurrect = true

	practice.hostiles = {}
	for _, ref in ipairs(scenario.actors or {}) do
		local actor = spawn_practice_enemy(ref)
		if actor then practice.hostiles[#practice.hostiles+1] = actor end
	end

	if #practice.hostiles == 0 then
		finish_practice("Simulation failed.", "error", "No boss actors were spawned.", scenario.template_label or "")
		return
	end

	if scenario.mode == "auto" then
		game.player._codex_practice_auto = true
		game.paused = false
	else
		game.player._codex_practice_auto = nil
		game.log("#LIGHT_BLUE#Codex practice arena ready: %s", scenario.template_label or "Practice Fight")
	end
end

local function start_practice()
	local scenario = load_scenario()
	if not scenario or game._codex_practice then return end

	game._codex_practice = {
		scenario = scenario,
		turns = 0,
		finished = false,
		hostiles = {},
		damage_events = {},
	}
	install_damage_trace()

	game:onLevelLoad("codex-practice-arena-1", enter_practice_arena)
	game:changeLevel(1, "codex-practice-arena", {direct_switch=true, temporary_zone_shift=true})
end

class:bindHook("ToME:birthDone", function(self, data)
	start_practice()
end)

class:bindHook("ToME:run", function(self, data)
	if not game or game._codex_practice or game._codex_practice_start_queued then return end
	if not load_scenario() then return end

	game._codex_practice_start_queued = true
	game:onTickEnd(function()
		start_practice()
	end)
end)
