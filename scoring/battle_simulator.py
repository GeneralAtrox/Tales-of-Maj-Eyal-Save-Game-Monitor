from __future__ import annotations

from dataclasses import dataclass, field

from scoring.combat_advice import AdviceItem, survive_one_hit_advice
from scoring.enemy_threat import EnemyOffense, PlayerDefenses, ThreatReport, weapon_threat

COMMON_DAMAGE_TYPES: tuple[str, ...] = (
    "all",
    "PHYSICAL",
    "FIRE",
    "COLD",
    "LIGHTNING",
    "ACID",
    "NATURE",
    "ARCANE",
    "LIGHT",
    "DARKNESS",
    "BLIGHT",
    "TEMPORAL",
    "MIND",
    "STEAM",
)

_PLAYER_DICT_FIELDS = ("resists", "resists_pen", "resists_cap")
_ENEMY_DICT_FIELDS = ("inc_damage", "resists_pen")


@dataclass(slots=True)
class BattleEnemySnapshot:
    name: str = ""
    level: float = 0.0
    life: float = 0.0
    max_life: float = 0.0
    rank_label: str = ""
    faction: str = ""
    type_name: str = ""
    subtype: str = ""
    template_location: str = ""
    template_level_label: str = ""
    template_quest: str = ""
    template_warning: str = ""
    offense: EnemyOffense = field(default_factory=EnemyOffense)


@dataclass(slots=True)
class BattleSimulationResult:
    player: PlayerDefenses | None
    enemy: BattleEnemySnapshot | None
    report: ThreatReport | None
    advice: list[AdviceItem]
    status: str = ""


def copy_player_defenses(player: PlayerDefenses | None) -> PlayerDefenses | None:
    if player is None:
        return None
    return PlayerDefenses(
        max_life=player.max_life,
        die_at=player.die_at,
        armor=player.armor,
        armor_hardiness_pct=player.armor_hardiness_pct,
        defense=player.defense,
        evasion_pct=player.evasion_pct,
        resists=dict(player.resists),
        resists_pen=dict(player.resists_pen),
        resists_cap=dict(player.resists_cap),
        ignore_direct_crits_pct=player.ignore_direct_crits_pct,
    )


def copy_enemy_snapshot(enemy: BattleEnemySnapshot | None) -> BattleEnemySnapshot | None:
    if enemy is None:
        return None
    offense = enemy.offense
    return BattleEnemySnapshot(
        name=enemy.name,
        level=enemy.level,
        life=enemy.life,
        max_life=enemy.max_life,
        rank_label=enemy.rank_label,
        faction=enemy.faction,
        type_name=enemy.type_name,
        subtype=enemy.subtype,
        template_location=enemy.template_location,
        template_level_label=enemy.template_level_label,
        template_quest=enemy.template_quest,
        template_warning=enemy.template_warning,
        offense=EnemyOffense(
            name=offense.name,
            rank=offense.rank,
            global_speed=offense.global_speed,
            atk=offense.atk,
            dam=offense.dam,
            apr=offense.apr,
            crit_chance_pct=offense.crit_chance_pct,
            crit_power_bonus_pct=offense.crit_power_bonus_pct,
            physspeed=offense.physspeed,
            inc_damage=dict(offense.inc_damage),
            resists_pen=dict(offense.resists_pen),
            talent_max_weapon_mult=offense.talent_max_weapon_mult,
        ),
    )


@dataclass(slots=True)
class BattleSimulatorState:
    player_live: PlayerDefenses | None = None
    player_base: PlayerDefenses | None = None
    player_overrides: dict[str, float] = field(default_factory=dict)
    player_dict_overrides: dict[str, dict[str, float]] = field(
        default_factory=lambda: {field_name: {} for field_name in _PLAYER_DICT_FIELDS}
    )
    enemy_base: BattleEnemySnapshot | None = None
    enemy_overrides: dict[str, str | float] = field(default_factory=dict)
    enemy_offense_overrides: dict[str, float] = field(default_factory=dict)
    enemy_dict_overrides: dict[str, dict[str, float]] = field(
        default_factory=lambda: {field_name: {} for field_name in _ENEMY_DICT_FIELDS}
    )

    def set_live_player(self, player: PlayerDefenses | None) -> None:
        self.player_live = copy_player_defenses(player)
        if self.player_live is None:
            return
        if self.player_base is None or not self.player_is_dirty:
            self.player_base = copy_player_defenses(self.player_live)

    @property
    def player_is_dirty(self) -> bool:
        return bool(self.player_overrides) or any(self.player_dict_overrides.values())

    def reset_player_to_live(self) -> None:
        self.player_overrides.clear()
        for overrides in self.player_dict_overrides.values():
            overrides.clear()
        self.player_base = copy_player_defenses(self.player_live)

    def load_enemy(self, enemy: BattleEnemySnapshot | None) -> None:
        self.enemy_base = copy_enemy_snapshot(enemy)
        self.enemy_overrides.clear()
        self.enemy_offense_overrides.clear()
        for overrides in self.enemy_dict_overrides.values():
            overrides.clear()

    def clear_enemy(self) -> None:
        self.enemy_base = None
        self.enemy_overrides.clear()
        self.enemy_offense_overrides.clear()
        for overrides in self.enemy_dict_overrides.values():
            overrides.clear()

    def set_player_scalar(self, field_name: str, value: float) -> None:
        self.player_overrides[field_name] = float(value)

    def set_player_damage_value(self, group: str, damage_type: str, value: float) -> None:
        self.player_dict_overrides.setdefault(group, {})[damage_type] = float(value)

    def set_enemy_scalar(self, field_name: str, value: str | float) -> None:
        self.enemy_overrides[field_name] = value

    def set_enemy_offense_scalar(self, field_name: str, value: float) -> None:
        self.enemy_offense_overrides[field_name] = float(value)

    def set_enemy_damage_value(self, group: str, damage_type: str, value: float) -> None:
        self.enemy_dict_overrides.setdefault(group, {})[damage_type] = float(value)

    def resolved_player(self) -> PlayerDefenses | None:
        if self.player_base is None:
            return None
        player = copy_player_defenses(self.player_base)
        assert player is not None
        for field_name, value in self.player_overrides.items():
            setattr(player, field_name, value)
        for field_name, overrides in self.player_dict_overrides.items():
            setattr(player, field_name, self._merged_dict(getattr(player, field_name), overrides))
        return player

    def resolved_enemy(self) -> BattleEnemySnapshot | None:
        if self.enemy_base is None:
            return None
        enemy = copy_enemy_snapshot(self.enemy_base)
        assert enemy is not None
        for field_name, value in self.enemy_overrides.items():
            setattr(enemy, field_name, value)
        for field_name, value in self.enemy_offense_overrides.items():
            setattr(enemy.offense, field_name, value)
        for field_name, overrides in self.enemy_dict_overrides.items():
            setattr(enemy.offense, field_name, self._merged_dict(getattr(enemy.offense, field_name), overrides))
        enemy.offense.name = enemy.name
        return enemy

    def compute(self) -> BattleSimulationResult:
        player = self.resolved_player()
        enemy = self.resolved_enemy()
        if player is None:
            return BattleSimulationResult(
                player=None,
                enemy=enemy,
                report=None,
                advice=[],
                status="Live player defenses are unavailable. Attach to a game or wait for a fresh snapshot.",
            )
        if enemy is None:
            return BattleSimulationResult(
                player=player,
                enemy=None,
                report=None,
                advice=[],
                status="Select a monster in Enemies and add it to the battle simulator.",
            )
        report = weapon_threat(enemy.offense, player)
        advice = survive_one_hit_advice(enemy.offense, player)
        return BattleSimulationResult(
            player=player,
            enemy=enemy,
            report=report,
            advice=advice,
            status="",
        )

    @staticmethod
    def _merged_dict(base: dict[str, float], overrides: dict[str, float]) -> dict[str, float]:
        merged = dict(base)
        merged.update(overrides)
        return merged
