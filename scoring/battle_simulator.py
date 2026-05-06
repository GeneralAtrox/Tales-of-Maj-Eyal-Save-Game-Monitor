from __future__ import annotations

from dataclasses import dataclass, field

from scoring.combat_advice import AdviceItem, survive_one_hit_advice
from scoring.enemy_threat import EnemyOffense, PlayerDefenses, ThreatReport, WeaponOffense, weapon_threat
from scoring.talent_threat import EnemyPowers, TalentThreatReport, compute_talent_threat

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
    powers: EnemyPowers = field(default_factory=EnemyPowers)


@dataclass(slots=True)
class BattleSimulationResult:
    player: PlayerDefenses | None
    enemy: BattleEnemySnapshot | None
    report: ThreatReport | None
    advice: list[AdviceItem]
    talent_report: TalentThreatReport | None = None
    status: str = ""


def threat_tier_label(threat_pct: float) -> str:
    if threat_pct >= 70:
        return "Deadly"
    if threat_pct >= 35:
        return "High"
    if threat_pct >= 20:
        return "Mediocre"
    return "Low"


def combined_threat_pct(report: ThreatReport | None, talent_report: TalentThreatReport | None) -> float:
    talent_pct = talent_report.max_available_threat_pct if talent_report is not None else 0.0
    weapon_pct = report.weapon_threat_pct if report is not None else 0.0
    return max(weapon_pct, talent_pct)


def threat_damage_type_label(report: ThreatReport) -> str:
    damage_types = report.damage_types or (report.damage_type,)
    if len(damage_types) == 1:
        return f"{report.worst_resist_type}  (x{report.worst_resist_multiplier:.2f})"
    return f"{', '.join(damage_types)}  (base {report.worst_resist_type} x{report.worst_resist_multiplier:.2f})"


@dataclass(frozen=True, slots=True)
class BattleCalibrationEstimate:
    expected_damage: float | None = None
    peak_damage: float | None = None
    damage_types: tuple[str, ...] = ()


def battle_calibration_estimate(result: BattleSimulationResult) -> BattleCalibrationEstimate:
    """Return the quick-sim damage values that should be compared with engine practice logs."""
    expected_candidates: list[tuple[float, str]] = []
    peak_candidates: list[tuple[float, str]] = []
    damage_types: list[str] = []
    if result.report is not None:
        _add_damage_candidate(expected_candidates, result.report.expected_damage, result.report.damage_type)
        _add_damage_candidate(peak_candidates, result.report.peak_damage, result.report.damage_type)
        for damage_type in result.report.damage_types or (result.report.damage_type,):
            _add_unique_damage_type(damage_types, damage_type)
    if result.talent_report is not None:
        talent_entry = result.talent_report.strongest_available_entry()
        if talent_entry is not None and talent_entry.expected_damage > 0.0:
            _add_damage_candidate(expected_candidates, talent_entry.expected_damage, talent_entry.damage_type)
            _add_damage_candidate(peak_candidates, talent_entry.expected_damage, talent_entry.damage_type)
            _add_unique_damage_type(damage_types, talent_entry.damage_type)
    expected = max(expected_candidates, default=None, key=lambda item: item[0])
    peak = max(peak_candidates, default=None, key=lambda item: item[0])
    return BattleCalibrationEstimate(
        expected_damage=expected[0] if expected is not None else None,
        peak_damage=peak[0] if peak is not None else None,
        damage_types=tuple(damage_types),
    )


def _add_damage_candidate(candidates: list[tuple[float, str]], damage: float, damage_type: str) -> None:
    if damage > 0.0:
        candidates.append((damage, damage_type))


def _add_unique_damage_type(damage_types: list[str], damage_type: str) -> None:
    normalized = damage_type.strip().upper()
    if normalized and normalized not in damage_types:
        damage_types.append(normalized)


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
        combat_crit_reduction_pct=player.combat_crit_reduction_pct,
        x=player.x,
        y=player.y,
    )


def _copy_weapon_offense(weapon: WeaponOffense) -> WeaponOffense:
    return WeaponOffense(
        source=weapon.source,
        atk=weapon.atk,
        dam=weapon.dam,
        apr=weapon.apr,
        crit_chance_pct=weapon.crit_chance_pct,
        crit_power_bonus_pct=weapon.crit_power_bonus_pct,
        accuracy_effect=weapon.accuracy_effect,
        accuracy_effect_scale=weapon.accuracy_effect_scale,
        damage_range=weapon.damage_range,
        physspeed=weapon.physspeed,
        damage_type=weapon.damage_type,
        damage_mult=weapon.damage_mult,
        project_damage_mult=weapon.project_damage_mult,
        inc_damage=dict(weapon.inc_damage),
        resists_pen=dict(weapon.resists_pen),
        melee_project=dict(weapon.melee_project),
        burst_on_hit=dict(weapon.burst_on_hit),
        burst_on_crit=dict(weapon.burst_on_crit),
        talent_procs=tuple(weapon.talent_procs),
        unmodeled_proc_hooks=tuple(weapon.unmodeled_proc_hooks),
    )


def copy_enemy_snapshot(enemy: BattleEnemySnapshot | None) -> BattleEnemySnapshot | None:
    if enemy is None:
        return None
    offense = enemy.offense
    powers = enemy.powers
    offhands = tuple(_copy_weapon_offense(weapon) for weapon in offense.offhands)
    offhand = offhands[0] if offhands else (_copy_weapon_offense(offense.offhand) if offense.offhand else None)
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
            accuracy_effect=offense.accuracy_effect,
            accuracy_effect_scale=offense.accuracy_effect_scale,
            damage_range=offense.damage_range,
            physspeed=offense.physspeed,
            weapon_range=offense.weapon_range,
            damage_type=offense.damage_type,
            inc_damage=dict(offense.inc_damage),
            resists_pen=dict(offense.resists_pen),
            melee_project=dict(offense.melee_project),
            burst_on_hit=dict(offense.burst_on_hit),
            burst_on_crit=dict(offense.burst_on_crit),
            talent_procs=tuple(offense.talent_procs),
            unmodeled_proc_hooks=tuple(offense.unmodeled_proc_hooks),
            spellpower=offense.spellpower,
            mindpower=offense.mindpower,
            physicalpower=offense.physicalpower,
            spell_crit_pct=offense.spell_crit_pct,
            mind_crit_pct=offense.mind_crit_pct,
            stats=dict(offense.stats),
            offhand=offhand,
            offhands=offhands or ((offhand,) if offhand else ()),
            talents=dict(offense.talents),
            talents_cd=dict(offense.talents_cd),
            resources=dict(offense.resources),
            has_resource_snapshot=offense.has_resource_snapshot,
            x=offense.x,
            y=offense.y,
            talent_max_weapon_mult=offense.talent_max_weapon_mult,
            talent_burst_weapon_mult=offense.talent_burst_weapon_mult,
            talent_burst_weapon_hits=offense.talent_burst_weapon_hits,
        ),
        powers=EnemyPowers(
            spellpower=powers.spellpower,
            mindpower=powers.mindpower,
            physicalpower=powers.physicalpower,
            global_speed=powers.global_speed,
            weapon_range=powers.weapon_range,
            atk=powers.atk,
            dam=powers.dam,
            apr=powers.apr,
            inc_damage=dict(powers.inc_damage),
            resists_pen=dict(powers.resists_pen),
            talents=dict(powers.talents),
            talents_cd=dict(powers.talents_cd),
            resources=dict(powers.resources),
            has_resource_snapshot=powers.has_resource_snapshot,
            x=powers.x,
            y=powers.y,
            stats=dict(powers.stats),
            spell_crit_pct=powers.spell_crit_pct,
            mind_crit_pct=powers.mind_crit_pct,
            physical_crit_pct=powers.physical_crit_pct,
            crit_power_bonus_pct=powers.crit_power_bonus_pct,
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
    enemy_offense_overrides: dict[str, str | float] = field(default_factory=dict)
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

    def set_enemy_offense_text(self, field_name: str, value: str) -> None:
        self.enemy_offense_overrides[field_name] = value

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
        enemy.powers.global_speed = enemy.offense.global_speed
        enemy.powers.weapon_range = enemy.offense.weapon_range
        enemy.powers.atk = enemy.offense.atk
        enemy.powers.dam = enemy.offense.dam
        enemy.powers.apr = enemy.offense.apr
        enemy.powers.inc_damage = self._merged_dict(enemy.powers.inc_damage, enemy.offense.inc_damage)
        enemy.powers.resists_pen = self._merged_dict(enemy.powers.resists_pen, enemy.offense.resists_pen)
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
        talent_report = compute_talent_threat(enemy.powers, player)
        advice = survive_one_hit_advice(enemy.offense, player)
        return BattleSimulationResult(
            player=player,
            enemy=enemy,
            report=report,
            advice=advice,
            talent_report=talent_report,
            status="",
        )

    @staticmethod
    def _merged_dict(base: dict[str, float], overrides: dict[str, float]) -> dict[str, float]:
        merged = dict(base)
        merged.update(overrides)
        return merged
