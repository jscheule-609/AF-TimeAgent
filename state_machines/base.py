"""
Abstract base class for jurisdiction-specific regulatory state machines.

Each state machine defines:
1. States — the regulatory stages
2. Transitions — the possible paths between states with base probabilities
3. Adjustment functions — how deal-specific factors modify transition probabilities
4. Duration distributions — how long each state takes (p50/p75/p90)
"""
from abc import ABC, abstractmethod
from collections import deque
from models.state_machine import (
    StateMachineState, StateTransition, PathOutcome,
    JurisdictionSimulation, JurisdictionName,
)
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from datetime import date
from typing import Optional


class BaseRegulatoryStateMachine(ABC):

    @abstractmethod
    def jurisdiction(self) -> JurisdictionName:
        pass

    @abstractmethod
    def define_states(self) -> list[StateMachineState]:
        pass

    @abstractmethod
    def define_transitions(
        self,
        overlap: OverlapAssessment,
        climate: RegulatoryClimate,
        comparable_stats: dict,
    ) -> list[StateTransition]:
        pass

    @abstractmethod
    def compute_duration_distributions(
        self,
        comparable_stats: dict,
        climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        """Returns: {state_id: {"p50": int, "p75": int, "p90": int}}"""
        pass

    @abstractmethod
    def terminal_states(self) -> set[str]:
        """Return set of terminal state IDs."""
        pass

    def simulate(
        self,
        announcement_date: date,
        overlap: OverlapAssessment,
        climate: RegulatoryClimate,
        comparable_stats: dict,
        contractual_filing_deadline_days: Optional[int] = None,
    ) -> JurisdictionSimulation:
        """Run the full simulation for this jurisdiction."""
        states = self.define_states()
        transitions = self.define_transitions(overlap, climate, comparable_stats)
        durations = self.compute_duration_distributions(comparable_stats, climate)

        # Apply durations to states
        for state in states:
            if state.state_id in durations:
                d = durations[state.state_id]
                state.duration_p50_days = d.get("p50", 0)
                state.duration_p75_days = d.get("p75", 0)
                state.duration_p90_days = d.get("p90", 0)
                state.expected_duration_days = d.get("p50", 0)

        # Enumerate paths
        paths = self.enumerate_paths(transitions)

        # Build PathOutcome for each path
        duration_map = {s.state_id: s for s in states}
        possible_paths = []
        terminals = self.terminal_states()
        clear_terminals = self._clear_terminal_states()

        for i, path_states in enumerate(paths):
            # Compute path probability (product of transition probs)
            prob = 1.0
            for j in range(len(path_states) - 1):
                t = self._find_transition(transitions, path_states[j], path_states[j + 1])
                if t:
                    prob *= t.probability

            # Compute durations along path
            p50 = sum(duration_map[s].duration_p50_days for s in path_states if s in duration_map)
            p75 = sum(duration_map[s].duration_p75_days for s in path_states if s in duration_map)
            p90 = sum(duration_map[s].duration_p90_days for s in path_states if s in duration_map)

            terminal = path_states[-1] if path_states else ""
            is_clear = terminal in clear_terminals

            possible_paths.append(PathOutcome(
                path_id=f"{self.jurisdiction().value.lower()}_path_{i}",
                path_label=self._path_label(path_states),
                states=path_states,
                total_duration_days_p50=p50,
                total_duration_days_p75=p75,
                total_duration_days_p90=p90,
                path_probability=prob,
                is_terminal_clear=is_clear,
            ))

        # Sort by probability descending
        possible_paths.sort(key=lambda p: p.path_probability, reverse=True)

        # Compute expected durations (probability-weighted)
        total_prob = sum(p.path_probability for p in possible_paths) or 1.0
        exp_p50 = int(sum(p.total_duration_days_p50 * p.path_probability for p in possible_paths) / total_prob)
        exp_p75 = int(sum(p.total_duration_days_p75 * p.path_probability for p in possible_paths) / total_prob)
        exp_p90 = int(sum(p.total_duration_days_p90 * p.path_probability for p in possible_paths) / total_prob)

        filing_deadline = None
        if contractual_filing_deadline_days is not None:
            from datetime import timedelta
            filing_deadline = announcement_date + timedelta(days=contractual_filing_deadline_days)

        return JurisdictionSimulation(
            jurisdiction=self.jurisdiction(),
            is_required=True,
            confidence_required=1.0,
            source_of_requirement="state_machine",
            states=states,
            transitions=transitions,
            possible_paths=possible_paths,
            expected_duration_days_p50=exp_p50,
            expected_duration_days_p75=exp_p75,
            expected_duration_days_p90=exp_p90,
            contractual_filing_deadline=filing_deadline,
            contractual_filing_deadline_days=contractual_filing_deadline_days,
        )

    def enumerate_paths(
        self,
        transitions: list[StateTransition],
        start_state: str = "not_filed",
        min_probability: float = 0.01,
    ) -> list[list[str]]:
        """
        Iterative BFS enumeration of all paths from start to any terminal state.
        Prune paths with cumulative probability < 1%.
        """
        terminals = self.terminal_states()
        # Build adjacency map
        adj: dict[str, list[StateTransition]] = {}
        for t in transitions:
            adj.setdefault(t.from_state, []).append(t)

        complete_paths = []
        # BFS: (current_path, cumulative_probability)
        queue = deque([([start_state], 1.0)])

        while queue:
            path, prob = queue.popleft()
            current = path[-1]

            if current in terminals:
                complete_paths.append(path)
                continue

            next_transitions = adj.get(current, [])
            if not next_transitions:
                complete_paths.append(path)
                continue

            for t in next_transitions:
                new_prob = prob * t.probability
                if new_prob < min_probability:
                    continue
                if t.to_state in path:  # Cycle detection (except pull-and-refile)
                    if t.to_state != "filed" or path.count("filed") > 1:
                        continue
                queue.append((path + [t.to_state], new_prob))

        return complete_paths

    def _find_transition(
        self, transitions: list[StateTransition], from_s: str, to_s: str
    ) -> Optional[StateTransition]:
        for t in transitions:
            if t.from_state == from_s and t.to_state == to_s:
                return t
        return None

    def _clear_terminal_states(self) -> set[str]:
        """Override to define which terminal states mean 'cleared'."""
        return {s for s in self.terminal_states() if "clear" in s or "cleared" in s}

    def _path_label(self, path_states: list[str]) -> str:
        """Generate a human-readable label for a path."""
        if not path_states:
            return "Empty path"
        terminal = path_states[-1]
        key_states = [s for s in path_states if s not in ("not_filed", "filed")]
        if "second_request" in terminal or "second_request" in str(key_states):
            return "Second Request Path"
        if "phase_2" in terminal or "phase_2" in str(key_states):
            return "Phase 2 Extended Review"
        if "blocked" in terminal or "prohibited" in terminal:
            return "Blocked / Prohibited"
        if "withdrawn" in terminal or "abandoned" in terminal:
            return "Withdrawn / Abandoned"
        if "clear" in terminal:
            return "Clean Clearance"
        return f"Path → {terminal}"
