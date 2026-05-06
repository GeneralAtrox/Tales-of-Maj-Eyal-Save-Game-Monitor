return {
	name = "Codex Practice Arena",
	level_range = {1, 100},
	level_scheme = "player",
	max_level = 1,
	width = 15,
	height = 15,
	all_remembered = true,
	all_lited = true,
	no_worldport = true,
	actor_adjust_level = function(zone, level, e)
		return game.player and game.player.level or level.level or 1
	end,
	generator = {
		map = {
			class = "engine.generator.map.Static",
			map = "zones/codex-practice-arena",
			zoom = 5,
		},
		actor = { },
	},
}
