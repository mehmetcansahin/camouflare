from __future__ import annotations

from typing import Any

import pytest

from camouflare.captcha import ClickSolverProvider, _active_solvers
from camouflare.models import V1Request
from camouflare.timer import TimeoutTimer
from tests.fakes import FakeContext


class FakeClickSolver:
    """Async-context double for playwright_captcha's ClickSolver."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.entered = False
        self.exited = False
        self.solve_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeClickSolver:
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.exited = True
        return False

    async def solve_captcha(
        self, *, captcha_container: Any, captcha_type: Any, **kwargs: Any
    ) -> bool:
        self.solve_calls.append(
            {"captcha_container": captcha_container, "captcha_type": captcha_type, **kwargs}
        )
        return True


@pytest.mark.anyio
async def test_click_solver_provider_prepares_and_solves_via_injected_solver() -> None:
    context = FakeContext()
    page = await context.new_page()
    created: list[FakeClickSolver] = []

    def factory(target_page: Any) -> FakeClickSolver:
        solver = FakeClickSolver(target_page)
        created.append(solver)
        return solver

    sentinel_type = object()
    provider = ClickSolverProvider(
        solver_factory=factory,
        captcha_type=sentinel_type,
        wait_checkbox_attempts=2,
        wait_checkbox_delay=0.25,
    )
    request = V1Request(cmd="request.get", url="https://example.com")

    async with provider.prepare(page=page):
        assert _active_solvers.get(page) is created[0]
        token = await provider.solve(page=page, request=request, timer=TimeoutTimer(60000))

    assert token is None
    solver = created[0]
    assert solver.entered is True
    assert solver.exited is True
    assert len(solver.solve_calls) == 1
    call = solver.solve_calls[0]
    assert call["captcha_container"] is page
    assert call["captcha_type"] is sentinel_type
    assert call["wait_checkbox_attempts"] == 2
    assert call["wait_checkbox_delay"] == 0.25
    # The per-page registry is cleaned up once prepare() exits.
    assert page not in _active_solvers


@pytest.mark.anyio
async def test_click_solver_provider_swallows_solve_error() -> None:
    class ExplodingSolver(FakeClickSolver):
        async def solve_captcha(
            self, *, captcha_container: Any, captcha_type: Any, **kwargs: Any
        ) -> bool:
            raise RuntimeError("Cloudflare iframes not found")

    context = FakeContext()
    page = await context.new_page()
    provider = ClickSolverProvider(
        solver_factory=lambda p: ExplodingSolver(p), captcha_type=object()
    )

    async with provider.prepare(page=page):
        # A solver that cannot find/click the widget must not raise; the request
        # falls back to waiting for the challenge to clear.
        token = await provider.solve(
            page=page,
            request=V1Request(cmd="request.get", url="https://example.com"),
            timer=TimeoutTimer(60000),
        )

    assert token is None


@pytest.mark.anyio
async def test_click_solver_provider_solve_without_prepare_is_noop() -> None:
    context = FakeContext()
    page = await context.new_page()
    provider = ClickSolverProvider(
        solver_factory=lambda p: FakeClickSolver(p),
        captcha_type=object(),
    )

    token = await provider.solve(
        page=page,
        request=V1Request(cmd="request.get", url="https://example.com"),
        timer=TimeoutTimer(60000),
    )

    assert token is None
    assert page not in _active_solvers
