import asyncio
import random
import pytz
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Any

import discord
from discord import Member, Message, VoiceState, VoiceChannel, TextChannel, Guild, Role
from discord.ext import commands
from discord.ext.commands import Context
from discord.ext.tasks import loop

from config import DiscordConfig, GithubConfig
from misc.functions import find, strdate2excel, strdate, first
from google.utils import Google, Creds
from dc.scoreboard import ScoreboardMessage, SCOREBOARD_SIGNATURE
from settings import DISCORD_CONFIG_FILEPATH, GITHUB_CONFIG_FILEPATH, GOOGLE_CREDENTIALS_FILEPATH, \
    CHECK_ON_EMPTY_VOICE_CHANNEL, CYRILLIC_ALPHABET, SPACE_SET, DRY_SHEET_RUN, DAILY_TASK_CHECK_PERIOD, \
    TZ_INFO, PENALTY_POINTS
from google.sheet import Sheet
from dc.utils import playgame, Timecoder, check_timecodes, THREAD_PHRASES
from github.utils import GithubWebhook

dc_cfg = DiscordConfig.load(DISCORD_CONFIG_FILEPATH)
github_cfg = GithubConfig.load(GITHUB_CONFIG_FILEPATH)

intents = discord.Intents.all()

bot = commands.Bot(
    command_prefix=dc_cfg.command_prefix,
    status=discord.Status.online,
    intents=intents,
    activity=None)


async def scoreboard_message_lookup(channel: TextChannel) -> Optional[Message]:
    pins: List[Message] = await channel.pins()
    sorted_pins = sorted(pins, key=lambda it: it.created_at, reverse=True)
    return find(lambda it: ScoreboardMessage.is_resemble(it.clean_content), sorted_pins)


async def scoreboard_sheet_load(channel: TextChannel) -> Tuple[Sheet, Message]:
    message = await scoreboard_message_lookup(channel)
    assert message is not None, f"Pinned message with scoreboard signature '{SCOREBOARD_SIGNATURE}' not found"
    scoreboard = ScoreboardMessage.parse(message)
    document_id = Google.get_sheet_id(scoreboard.url)
    # sheet.fix_flags([TIME_CODES_COLUMN_INDEX])  # not yet needed cus of render option
    creds = Creds.service(GOOGLE_CREDENTIALS_FILEPATH)
    return Google(creds).load_sheet(document_id, scoreboard.cells, scoreboard.url), message


async def get_voice_channel(ctx: Context) -> VoiceChannel:
    voice: VoiceState = ctx.message.author.voice
    assert voice is not None, "Not connected to any voice channel, please connect"
    return voice.channel


async def get_voice_desc(ctx: Context) -> Tuple[VoiceChannel, List[Member]]:
    voice_channel = await get_voice_channel(ctx)
    members: List[Member] = voice_channel.members
    assert not CHECK_ON_EMPTY_VOICE_CHANNEL or len(members) != 0, \
        f"You are the only one in the voice channel '{voice_channel.name}' or nobody in channel, please reconnect"
    return voice_channel, members


def get_text_channel(ctx, channel_id: Optional[int] = None) -> TextChannel:
    if channel_id is not None:
        return bot.get_channel(channel_id)
    else:
        return ctx.channel


@dataclass
class Entry:
    name: str
    value: Any


@bot.command(name="найди-нарушителей")
@commands.has_any_role(*dc_cfg.bot_command_roles_id)
@playgame(bot, None)
async def check_rules_violations(ctx: Optional[Context]):
    print("Check server rules violations")

    # TODO: make global or class-fields
    guild: Guild = bot.get_guild(dc_cfg.server_id)

    warnings_channel: TextChannel = bot.get_channel(dc_cfg.warnings_channel_id)
    # warnings_channel: TextChannel = bot.get_channel(940608098387779667)  # for tests

    rules_channel: TextChannel = bot.get_channel(dc_cfg.rules_channel_id)
    questions_channel: TextChannel = bot.get_channel(dc_cfg.questions_channel_id)

    undefined = guild.get_role(dc_cfg.undefined_role_id)
    everyone = guild.get_role(dc_cfg.everyone_role_id)
    dont_check_name = guild.get_role(dc_cfg.dont_check_name_role_id)

    privileged_roles = [guild.get_role(it) for it in dc_cfg.privileged_roles_id]

    for member in guild.members:
        member: Member = member

        if any(it in privileged_roles for it in member.roles):
            continue

        violations: List[Entry] = list()

        if undefined in member.roles or len(member.roles) == 1 and member.roles[0] == everyone:
            entry = Entry(f"Нарушение роли пользователя", f"Выберите Вашу группу в канале {questions_channel.mention}")
            violations.append(entry)

        if dont_check_name not in member.roles:
            invalid_letters = set(member.display_name) - CYRILLIC_ALPHABET - SPACE_SET
            if invalid_letters:
                entry = Entry(
                    "Нарушение имени пользователя",
                    "Укажите Ваше имя пользователя на сервере в формате: <Фамилия> <Имя>")
                violations.append(entry)

        if violations:
            root: Role = guild.get_role(dc_cfg.root_role_id)

            print(f"User '{member.display_name}' violated server rules")

            embed = discord.Embed(
                title=f"Нарушение правил сервера",
                description=f"{member.mention} обнаружены нарушения правил {rules_channel.mention}. "
                            f"Если Вы считаете, что всё окей, обратитесь к {root.mention} - бывает, что я ошибаюсь",
                color=0xff0000
            )

            embed.set_author(name="Исскуственный интелект")

            for index, violation in enumerate(violations):
                embed.add_field(name=f"{index + 1}. {violation.name}", value=violation.value)

            embed.set_footer(text=f"Исправьте эти нарушения иначе будете забанены.")

            await warnings_channel.send(embed=embed)


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord at {datetime.now()}!')


@bot.command(
    name="выбери-кто-соберет-таймкоды",
    help="Выбирает пользователя случайным образом у кого выставлен <V> в таймкодах"
)
@commands.has_any_role(*dc_cfg.bot_command_roles_id)
@playgame(bot, discord.Game(name="Выбираю, кто соберет таймкоды"))
async def choose_timecoder(ctx: Context, score: int = 2, channel_id: Optional[int] = None):
    text_channel = get_text_channel(ctx, channel_id)

    voice_channel, members = await get_voice_desc(ctx)

    sheet, pin = await scoreboard_sheet_load(text_channel)

    acquired_timecoders = sheet.get_timecoders()
    assert acquired_timecoders, f"Nobody want to be timecoder"

    present_users = [it.display_name for it in members]

    present_timecoders_names = list(set(acquired_timecoders) & set(present_users))
    assert present_timecoders_names, f"All possible timecoders are absent :("

    present_timecoders: List[Member] = \
        [first(lambda it: it.display_name == name, members) for name in present_timecoders_names]

    timecoder = random.choice(present_timecoders)

    date = strdate(ctx.message.created_at)

    timecoders.append(Timecoder(timecoder, ctx.message.created_at, text_channel, score))

    t = ctx.message.created_at
    timestamp = int((datetime(t.year, t.month, t.day, t.hour, t.minute) +
                     timedelta(hours=dc_cfg.timecodes_countdown_hours) - datetime(1970, 1, 1)).total_seconds())

    await ctx.reply(f"Исскуственный интелект назначил {timecoder.mention} ответственным за таймкоды **{date}**, "
                    f"по {voice_channel.mention}. Ты получишь +{score} баллов, если успешно справишься "
                    f"с задачей и {PENALTY_POINTS} в противном случае. "
                    f"Я выбирал между {', '.join(it.mention for it in present_timecoders)} отсюда {pin.jump_url}.\n"
                    f"Пример сообщения с таймкодами:\n```\n00:00 - Timecode#1\n05:00 - Timecode#2\n...\n```\n"
                    f"Событие завершится <t:{timestamp}:F>")


@bot.command(
    name="отметь-кто-тут",
    help="Обновляет score пользователей, в голосовом канале"
)
@commands.has_any_role(*dc_cfg.bot_command_roles_id)
@playgame(bot, discord.Game(name="Отмечаю, кто сидит в голосовом канале..."))
async def update_score(ctx: Context, date: Optional[str] = None, score: int = 1, channel_id: Optional[int] = None):
    text_channel = get_text_channel(ctx, channel_id)

    voice_channel, members = await get_voice_desc(ctx)

    assert score > 0, f"This is unfair to add {score}"

    sheet, pin = await scoreboard_sheet_load(text_channel)

    date = date or strdate(ctx.message.created_at)
    excel_date = strdate2excel(date)

    assert sheet.is_header_exists(excel_date), f"Date '{date}' not found in table headers"

    lines = []
    for member in members:
        new_score = sheet.add_score(member.display_name, excel_date, score)
        if new_score is not None:
            string = f"- Новый score пользователя {member.mention}: {new_score}"
        else:
            print(f"User not found: {member.display_name}")
            string = f"- Пользователь {member.mention} не был найден в Scoreboard :("
        lines.append(string)

    if not DRY_SHEET_RUN:
        creds = Creds.service(GOOGLE_CREDENTIALS_FILEPATH)
        Google(creds).store_sheet(sheet)

    message = f"Обновленный рейтинг за {date} по данным из {voice_channel.mention}:\n" + \
              "\n".join(lines) + f"\nScoreboard взял отсюда: {pin.jump_url}"

    await ctx.reply(message)


@bot.command(
    name="добавь-вебхук",
    help="Добавляет вебхук репозитория из гитхаба"
)
@commands.has_any_role(*dc_cfg.bot_command_roles_id)
@playgame(bot, discord.Game(name="Добавляю вебхук..."))
async def create_github_webhook(ctx: Context, channel_id: int, webhook_name: str, repo_name: str):
    threads = [it.threads for it in ctx.guild.text_channels]
    threads_id = [thread.id for lst in threads for thread in lst]
    channels_id = [x.id for x in ctx.guild.text_channels]

    if channel_id in channels_id:
        is_thread = False
    elif channel_id in threads_id:
        for it in ctx.guild.text_channels:
            if channel_id in [jt.id for jt in it.threads]:
                parent_channel_id = it.id
        is_thread = True
    else:
        print(f"Channel '{channel_id}' not in text channels or threads")
        return

    gw = GithubWebhook.from_data(dc_cfg, github_cfg)
    if is_thread:
        if not gw.create(webhook_name, parent_channel_id, repo_name, channel_id):
            return
    else:
        if not gw.create(webhook_name, channel_id, repo_name):
            return

    message = f"Вебхук \"{webhook_name}\" добавлен"

    await ctx.reply(message)


scheduled: Optional[datetime] = None
timecoders: List[Timecoder] = list()


@loop(seconds=DAILY_TASK_CHECK_PERIOD)
async def background_loop():
    global scheduled

    await bot.wait_until_ready()

    scheduled = scheduled or datetime.now(TZ_INFO).replace(
        hour=dc_cfg.daily_task_time.hour,
        minute=dc_cfg.daily_task_time.minute,
        second=dc_cfg.daily_task_time.second)

    now = datetime.now(TZ_INFO)
    guild = bot.get_guild(dc_cfg.server_id)

    for timecoder in timecoders:
        messages = [message async for message in timecoder.channel.history(limit=20)
                    if message.author == timecoder.member and message.created_at > timecoder.choice_time]
        message = None
        for m in messages:
            if check_timecodes(m.content):
                message = m

        is_late = timecoder.choice_time + timedelta(hours=dc_cfg.timecodes_countdown_hours) <= now
        if message or is_late:
            timecoders.remove(timecoder)
            sheet, pin = await scoreboard_sheet_load(timecoder.channel)
            date = strdate(timecoder.choice_time)
            excel_date = strdate2excel(date)
            sheet.add_score(timecoder.member.display_name, excel_date, PENALTY_POINTS if is_late else timecoder.score)
        if message:
            await message.reply(f"Добавил {timecoder.score} балла")
        elif is_late:
            await timecoder.channel.send(f"{timecoder.member.mention} К сожалению, таймкодов не найдено :(")

    if guild:
        threads = [thread for lst in guild.text_channels for thread in lst.threads]
        for thread in threads:
            if thread.id in dc_cfg.tracked_threads_id and not thread.archived:
                async for msg in thread.history(limit=1):
                    last_msg = msg

                archive_half_time = last_msg.created_at.replace(tzinfo=pytz.utc).astimezone(TZ_INFO) + \
                                    timedelta(minutes=thread.auto_archive_duration / 2)
                if now >= archive_half_time:
                    await thread.send(random.choice(THREAD_PHRASES))

    if now >= scheduled:
        scheduled += timedelta(days=1)
        print(f"Executing daily tasks, next scheduled {scheduled}")
        await check_rules_violations(None)
