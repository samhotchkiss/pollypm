from __future__ import annotations

import shutil

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pollypm.models import ProviderKind

from .onboarding_models import CliAvailability, ConnectedAccount, ProviderChoice


def available_clis() -> list[CliAvailability]:
    return [
        CliAvailability(
            provider=ProviderKind.CLAUDE,
            label="Claude CLI",
            binary="claude",
            installed=shutil.which("claude") is not None,
        ),
        CliAvailability(
            provider=ProviderKind.CODEX,
            label="Codex CLI",
            binary="codex",
            installed=shutil.which("codex") is not None,
        ),
    ]


def render_intro(statuses: list[CliAvailability]) -> None:
    console = Console()
    hero_text = Text(justify="left")
    hero_text.append("PollyPM\n", style="bold bright_white")
    hero_text.append(
        "Set up your CLI agents, detect active projects, and bring the control room online.",
        style="white",
    )
    hero = Panel(
        hero_text,
        border_style="grey70",
        box=box.ROUNDED,
        padding=(1, 2),
    )
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("tool", style="bold")
    table.add_column("status")
    for item in statuses:
        state = "[green]Ready[/green]" if item.installed else "[red]Missing[/red]"
        table.add_row(item.label, state)
    table.add_row(
        "tmux",
        "[green]Ready[/green]" if shutil.which("tmux") else "[red]Missing[/red]",
    )
    machine_panel = Panel(
        table,
        title="Machine Check",
        border_style="grey50",
        box=box.ROUNDED,
    )

    flight_plan = Table(show_header=False, box=None, pad_edge=False)
    flight_plan.add_column("step", style="bold cyan", width=4)
    flight_plan.add_column("detail")
    flight_plan.add_row("01", "Connect your first agent account")
    flight_plan.add_row("02", "Detect projects with recent activity")
    flight_plan.add_row("03", "Launch your control room")
    plan_panel = Panel(
        flight_plan,
        title="Setup Flow",
        subtitle="You can add more accounts later",
        border_style="grey50",
        box=box.ROUNDED,
    )

    console.print(hero)
    console.print(Columns([machine_panel, plan_panel], equal=True, expand=True))
    console.print()


def starting_provider_message(installed: list[CliAvailability]) -> str:
    if len(installed) == 1:
        return f"First, let's connect {installed[0].label}."
    return "First, let's connect your agent accounts."


def provider_choices(
    installed: list[CliAvailability],
    accounts: dict[str, ConnectedAccount],
) -> list[ProviderChoice]:
    connected_by_provider = {item.provider: 0 for item in installed}
    for account in accounts.values():
        connected_by_provider[account.provider] = (
            connected_by_provider.get(account.provider, 0) + 1
        )

    choices: list[ProviderChoice] = []
    for index, item in enumerate(installed, start=1):
        count = connected_by_provider.get(item.provider, 0)
        suffix = f" ({count} connected)" if count else ""
        choices.append(ProviderChoice(str(index), f"{item.label}{suffix}", item.provider))
    if accounts:
        choices.append(ProviderChoice(str(len(choices) + 1), "Continue", None))
    return choices


def render_account_step_intro(
    installed: list[CliAvailability],
    accounts: dict[str, ConnectedAccount],
) -> None:
    console = Console()
    body = Text()
    body.append(starting_provider_message(installed), style="bold white")
    body.append("\n")
    body.append(
        "PollyPM opens a real login window, detects the account automatically, and saves it as a reusable profile.",
        style="dim",
    )
    if not accounts:
        body.append("\n\n")
        body.append("Press Return to connect your first account.", style="bold bright_green")
    else:
        body.append("\n\n")
        body.append(
            "You can connect more now, or later anytime from the Accounts tab.",
            style="yellow",
        )
    console.print(
        Panel(body, title="Account Setup", border_style="grey50", box=box.ROUNDED)
    )


def render_connected_account(account: ConnectedAccount, total_accounts: int) -> None:
    console = Console()
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("Connected", account.email)
    table.add_row("Provider", account.provider.value)
    table.add_row("Profile", account.account_name)
    table.add_row("Accounts ready", str(total_accounts))
    console.print(
        Panel(table, title="Account Ready", border_style="green", box=box.ROUNDED)
    )


def provider_description(provider: ProviderKind) -> str:
    if provider is ProviderKind.CLAUDE:
        return "Strong for planning, review, and longer strategic loops."
    if provider is ProviderKind.CODEX:
        return "Strong for implementation, shell-heavy work, and fast coding loops."
    return ""


def render_provider_choices(choices: list[ProviderChoice]) -> None:
    console = Console()
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_header=True,
        header_style="bold white",
    )
    table.add_column("Option", width=8, style="bold cyan")
    table.add_column("Provider", width=18, style="bold")
    table.add_column("Best For")
    for item in choices:
        if item.provider is None:
            table.add_row(
                item.key,
                "Continue",
                "Move on to controller and project setup.",
            )
            continue
        table.add_row(item.key, item.label, provider_description(item.provider))
    console.print(
        Panel(
            table,
            title="Choose a Provider",
            border_style="grey50",
            box=box.ROUNDED,
        )
    )


__all__ = [
    "available_clis",
    "provider_choices",
    "render_account_step_intro",
    "render_connected_account",
    "render_intro",
    "render_provider_choices",
]
