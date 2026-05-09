import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import html
import asyncio
import json
import sys
import argparse
import stealth_requests
import shutil
import math
import random
from urllib.parse import urlparse, unquote
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple
import logging

logger = logging.getLogger("dga")

DEFAULT_UPLOAD_LIMIT = 25 * 1024 * 1024
OVERSIZE_FACTOR = 1.5
GIF_CONVERT_FPS = 24
GIF_CONVERT_MAX_WIDTH = 800
GIF_COMPRESS_FPS = 15
MAX_SEARCH_RESULTS = 5
DOWNLOAD_TIMEOUT = 30.0
OEMBED_TIMEOUT = 10


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


try:
    import ffmpeg
    from wand.image import Image as WandImage
except ImportError as e:
    print(f"CRITICAL ERROR: Required python package missing. ({e})")
    print("Please install them using: pip install ffmpeg-python Wand")
    sys.exit(1)


@dataclass
class AppConfig:
    bot_token: str
    target_channel_id: int

    @classmethod
    def load_from_file(cls, path: str) -> 'AppConfig':
        if not os.path.exists(path):
            logger.critical(f"Configuration file '{path}' not found.")
            logger.info("Please create the file with the following JSON format:\n{\n    \"bot_token\": \"YOUR_BOT_TOKEN_HERE\",\n    \"target_channel_id\": 123456789012345678\n}")
            sys.exit(1)

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                bot_token = data.get("bot_token", "").strip()
                target_channel_id = int(data.get("target_channel_id", 0))

                if not bot_token or not target_channel_id:
                    raise ValueError("Missing 'bot_token' or 'target_channel_id'")

                return cls(bot_token=bot_token, target_channel_id=target_channel_id)

        except Exception as e:
            logger.critical(f"Error loading {path}: {e}")
            sys.exit(1)


class DependencyValidator:
    @staticmethod
    def verify_system_requirements():
        if not shutil.which("ffmpeg"):
            logger.critical("'ffmpeg' command not found in your system PATH.")
            sys.exit(1)

        if not shutil.which("magick"):
            logger.critical("'magick' (ImageMagick) command not found in your system PATH.")
            sys.exit(1)


class InputValidator:
    MAX_LINK_LENGTH = 2048
    MAX_QUERY_LENGTH = 100
    ALLOWED_SCHEMES = {'http', 'https'}
    ALLOWED_IMAGE_EXTENSIONS = {
        '.gif', '.png', '.jpg', '.jpeg', '.webp',
        '.mp4', '.webm', '.mov', '.avif', '.apng',
    }

    @staticmethod
    def validate_link(link: str) -> str:
        link = link.strip()
        if not link:
            raise ValueError("Link cannot be empty.")
        if len(link) > InputValidator.MAX_LINK_LENGTH:
            raise ValueError("Link is too long (max 2048 characters).")

        parsed = urlparse(link)
        if parsed.scheme.lower() not in InputValidator.ALLOWED_SCHEMES:
            raise ValueError(f"Invalid URL scheme `{parsed.scheme or '(none)'}`. Only HTTP and HTTPS links are allowed.")
        if not parsed.netloc:
            raise ValueError("Invalid URL: missing domain.")

        return link

    @staticmethod
    def validate_image(image: discord.Attachment) -> None:
        if not image.filename or '.' not in image.filename:
            raise ValueError("Uploaded file has no recognizable extension.")

        ext = f".{image.filename.rsplit('.', 1)[-1].lower()}"
        if ext not in InputValidator.ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type `{ext}`. Allowed: "
                f"{', '.join(sorted(InputValidator.ALLOWED_IMAGE_EXTENSIONS))}"
            )

    @staticmethod
    def validate_name(name: str) -> str:
        """Sanitize a GIF name into a clean filename (e.g. 'why-is-he-lying-481.gif')."""
        name = name.strip()
        if not name:
            raise ValueError("Name cannot be empty.")
        if len(name) > 200:
            raise ValueError("Name is too long (max 200 characters).")

        sanitized = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        if not sanitized:
            raise ValueError("Name must contain at least one letter or number.")

        rand_suffix = random.randint(1, 1000)
        return f"{sanitized}-{rand_suffix}.gif"

    @staticmethod
    def validate_query(query: str) -> str:
        query = query.strip()
        if not query:
            raise ValueError("Search query cannot be empty.")
        if len(query) > InputValidator.MAX_QUERY_LENGTH:
            raise ValueError(f"Search query is too long (max {InputValidator.MAX_QUERY_LENGTH} characters).")
        return query


class MediaProcessor:
    MEDIA_TYPES = {
        'image/gif': '.gif', 'video/mp4': '.mp4', 'image/png': '.png',
        'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'video/webm': '.webm',
        'image/webp': '.webp', 'video/quicktime': '.mov',
        'image/avif': '.avif', 'image/apng': '.apng'
    }

    @staticmethod
    def get_magic_type(file_path: Path) -> str:
        try:
            with open(file_path, 'rb') as f:
                header = f.read(32)
        except Exception:
            return 'unknown'

        if header.startswith(b'GIF8'):
            return 'gif'
        if header.startswith(b'\x1aE\xdf\xa3'):
            return 'webm'
        if header.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'png'
        if header.startswith(b'\xff\xd8\xff'):
            return 'jpeg'
        if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
            return 'webp'

        if len(header) >= 12 and header[4:8] == b'ftyp':
            brand = header[8:12]
            if brand in (b'avif', b'avis') or b'avif' in header[16:32] or b'avis' in header[16:32]:
                return 'avif'
            elif brand in (b'qt  ',):
                return 'mov'
            else:
                return 'mp4'

        return 'unknown'

    @staticmethod
    def compress_gif(input_path: Path, target_size: int) -> Path:
        output_path = input_path.with_name(f"compressed_{input_path.name}")
        try:
            current_size = os.path.getsize(input_path)
            if current_size <= target_size:
                return input_path

            ratio = target_size / current_size

            scale_factor = math.sqrt(ratio) * 0.75
            scale_factor = max(0.1, min(1.0, scale_factor))

            colors = 256 if ratio > 0.4 else 128 if ratio > 0.2 else 64

            stream = ffmpeg.input(str(input_path))
            v = stream.video.filter('fps', fps=GIF_COMPRESS_FPS).filter('scale', w=f'trunc(iw*{scale_factor})', h='-1')
            split = v.split()

            palette = split[0].filter('palettegen', max_colors=colors)
            out = ffmpeg.filter([split[1], palette], 'paletteuse', dither='bayer', bayer_scale=5)

            (
                ffmpeg
                .output(out, str(output_path), loglevel="error")
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )
            if output_path.exists():
                return output_path
        except Exception as e:
            logger.error(f"Compression failed: {e}")

        return input_path

    @staticmethod
    def convert_to_gif(input_path: Path) -> Path:
        output_path = input_path.with_suffix('.gif')

        if input_path.suffix.lower() == '.gif':
            return input_path

        errors = []
        is_video = input_path.suffix.lower() in ['.mp4', '.webm', '.mov']

        def try_ffmpeg():
            try:
                if is_video:
                    stream = ffmpeg.input(str(input_path))
                    v = stream.video.filter('fps', fps=GIF_CONVERT_FPS).filter('scale', w=f'min(iw,{GIF_CONVERT_MAX_WIDTH})', h='-1')
                    split = v.split()
                    palette = split[0].filter('palettegen')
                    out = ffmpeg.filter([split[1], palette], 'paletteuse')
                    (
                        ffmpeg
                        .output(out, str(output_path), loglevel="error")
                        .overwrite_output()
                        .run(capture_stdout=True, capture_stderr=True)
                    )
                else:
                    (
                        ffmpeg
                        .input(str(input_path))
                        .output(str(output_path), loglevel="error")
                        .overwrite_output()
                        .run(capture_stdout=True, capture_stderr=True)
                    )
                return output_path.exists()
            except ffmpeg.Error as e:
                stderr_text = e.stderr.decode('utf-8', errors='ignore').strip() if e.stderr else "Unknown error"
                errors.append(f"FFmpeg failed: {stderr_text}")
            except Exception as e:
                errors.append(f"FFmpeg exception: {e}")
            return False

        def try_wand():
            try:
                with WandImage(filename=str(input_path)) as img:
                    img.format = 'gif'
                    img.save(filename=str(output_path))
                return output_path.exists()
            except Exception as e:
                errors.append(f"Magick failed: {e}")
            return False

        if is_video:
            if try_ffmpeg(): return output_path
            if try_wand(): return output_path
        else:
            if try_wand(): return output_path
            if try_ffmpeg(): return output_path

        raise RuntimeError(" | ".join(errors))


class URLResolver:
    @staticmethod
    async def _get_giphy_title(gif_id: str) -> str:
        if not re.match(r'^[a-zA-Z0-9]+$', gif_id):
            return "untitled"

        oembed_url = f"https://giphy.com/services/oembed/?url=https://giphy.com/gifs/{gif_id}"

        try:
            def _fetch_oembed():
                resp = stealth_requests.get(oembed_url, timeout=OEMBED_TIMEOUT)
                resp.raise_for_status()
                return resp.json()

            data = await asyncio.to_thread(_fetch_oembed)
            raw_title = data.get("title", "Untitled")

            clean_title = raw_title.split('-')[0].strip()
            if clean_title.upper().endswith(" GIF"):
                clean_title = clean_title[:-4].strip()

            return clean_title if clean_title else "untitled"
        except Exception:
            pass

        return "untitled"

    @staticmethod
    def _make_giphy_filename(title: str) -> str:
        sanitized = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        if not sanitized:
            sanitized = "untitled"
        rand_suffix = random.randint(1, 1000)
        return f"{sanitized}-{rand_suffix}.gif"

    @staticmethod
    async def resolve(url: str) -> Tuple[str, Optional[str]]:
        url = html.unescape(url)

        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if 'tenor.com' in domain and '/view/' in url:
            try:
                def _fetch_tenor():
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    return stealth_requests.get(url, headers=headers).text

                html_text = await asyncio.to_thread(_fetch_tenor)
                match_gif = re.search(r'content="(https://[^"]+\.tenor\.com/[^"]+\.gif)"', html_text)
                if match_gif: return match_gif.group(1), None

                match_mp4 = re.search(r'content="(https://[^"]+\.tenor\.com/[^"]+\.mp4)"', html_text)
                if match_mp4: return match_mp4.group(1), None
            except Exception as e:
                logger.error(f"Tenor resolve error: {e}")

        elif 'giphy.com' in domain and '/gifs/' in url:
            clean_path = parsed.path.strip('/')
            giphy_id = clean_path.split('/')[-1].split('-')[-1]
            direct_url = f"https://i.giphy.com/{giphy_id}.gif"

            title = await URLResolver._get_giphy_title(giphy_id)
            filename = URLResolver._make_giphy_filename(title)
            return direct_url, filename

        elif 'images-ext-' in domain:
            path_parts = parsed.path.split('/')
            for i, part in enumerate(path_parts):
                if part == 'external' and i + 1 < len(path_parts):
                    encoded_url = '/'.join(path_parts[i+1:]).split('?')[0]
                    try:
                        decoded = unquote(encoded_url)
                        if decoded.startswith('http'): return decoded, None
                    except Exception: pass

        return url, None


class ArchiverBot(commands.Bot):
    def __init__(self, config: AppConfig):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.app_config = config
        self.temp_dir = Path("temp_archive")
        self.temp_dir.mkdir(exist_ok=True)
        self.archive_index: list[dict] = []
        self._index_lock = asyncio.Lock()
        self.index_ready = asyncio.Event()
        self._initial_scan_done = False

    async def setup_hook(self):
        await self.add_cog(ArchiveCog(self))
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def on_ready(self):
        logger.info(f"Archiver Bot Online! Logged in as {self.user}")

        if self._initial_scan_done:
            logger.info("Reconnected — skipping archive re-scan (index preserved).")
            return

        try:
            channel = await self.fetch_target_channel()
            logger.info(f"Archive channel: #{channel.name} (ID: {channel.id})")
            logger.info("Scanning archive channel for GIF inventory...")

            message_count = 0
            attachment_count = 0
            total_size = 0
            index_entries: list[dict] = []

            async for msg in channel.history(limit=None):
                message_count += 1
                for att in msg.attachments:
                    attachment_count += 1
                    total_size += att.size
                    index_entries.append({
                        "message_id": msg.id,
                        "filename": att.filename,
                        "size": att.size,
                    })

            async with self._index_lock:
                self.archive_index = index_entries

            logger.info(f"Scan complete: {attachment_count} attachments across {message_count} messages")
            logger.info(f"Total archive size: {total_size / (1024 * 1024):.1f} MB")
        except Exception as e:
            logger.warning(f"Startup scan failed (non-fatal): {e}")
        finally:
            self._initial_scan_done = True
            self.index_ready.set()

    async def fetch_target_channel(self) -> discord.abc.Messageable:
        channel = self.get_channel(self.app_config.target_channel_id)
        if not channel:
            channel = await self.fetch_channel(self.app_config.target_channel_id)
        return channel

    async def download_from_link(self, link: str, interaction_id: int, max_size: int) -> Tuple[Path, str, Optional[str]]:
        resolved_url, suggested_name = await URLResolver.resolve(link)
        if not resolved_url:
            raise ValueError("Invalid URL provided.")

        def _download_file():
            return stealth_requests.get(resolved_url, timeout=DOWNLOAD_TIMEOUT)

        resp = await asyncio.to_thread(_download_file)
        if resp.status_code != 200:
            raise ValueError(f"Failed to download file. HTTP {resp.status_code}")

        if len(resp.content) > max_size * OVERSIZE_FACTOR:
            raise ValueError(
                f"**Download Rejected:** The source file ({len(resp.content) / (1024 * 1024):.1f} MB) "
                f"is too large. It exceeds {OVERSIZE_FACTOR:.0f}x the server's upload limit ({max_size / (1024 * 1024):.1f} MB)."
            )

        content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
        ext = MediaProcessor.MEDIA_TYPES.get(content_type, '.bin')
        if ext == '.bin':
            ext = '.' + resolved_url.split('?')[0].split('.')[-1]

        temp_file = self.temp_dir / f"temp_{interaction_id}{ext}"
        temp_file.write_bytes(resp.content)
        return temp_file, ext, suggested_name

    async def save_attachment(self, attachment: discord.Attachment, interaction_id: int, max_size: int) -> Path:
        if attachment.size > max_size * OVERSIZE_FACTOR:
            raise ValueError(
                f"**Upload Rejected:** The provided image ({attachment.size / (1024 * 1024):.1f} MB) "
                f"is too large. It exceeds {OVERSIZE_FACTOR:.0f}x the server's upload limit ({max_size / (1024 * 1024):.1f} MB)."
            )

        ext = f".{attachment.filename.split('.')[-1].lower()}" if '.' in attachment.filename else '.bin'
        temp_file = self.temp_dir / f"temp_{interaction_id}{ext}"
        await attachment.save(temp_file)
        return temp_file


class ArchiveCog(commands.Cog):

    def __init__(self, bot: ArchiverBot):
        self.bot = bot

    @app_commands.command(name="archive", description="Download, convert, and archive a GIF or image to your private server.")
    @app_commands.describe(
        name="A short name for this GIF (e.g. 'why is he lying')",
        link="The Tenor, Giphy, or Discord CDN link to archive",
        image="Upload an image to convert and archive",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def archive_command(self, interaction: discord.Interaction, name: str, link: str = None, image: discord.Attachment = None):
        await interaction.response.defer(ephemeral=True)

        async def safe_reply(content: str):
            try:
                if not interaction.is_expired():
                    await interaction.followup.send(content)
                else:
                    await interaction.user.send(content)
            except discord.errors.NotFound:
                try:
                    await interaction.user.send(content)
                except discord.errors.Forbidden:
                    pass
            except Exception:
                pass

        if bool(link) == bool(image):
            await safe_reply("❌ Please provide exactly **one** option: either a `link` or an `image`.")
            return

        # --- Input validation ---
        try:
            upload_filename = InputValidator.validate_name(name)
            if link:
                link = InputValidator.validate_link(link)
            if image:
                InputValidator.validate_image(image)
        except ValueError as e:
            await safe_reply(f"❌ {e}")
            return

        temp_file: Optional[Path] = None
        final_file: Optional[Path] = None
        cleanup_paths: list[Path] = []

        try:
            channel = await self.bot.fetch_target_channel()
            upload_limit = channel.guild.filesize_limit if hasattr(channel, 'guild') else DEFAULT_UPLOAD_LIMIT

            if link:
                temp_file, _, _ = await self.bot.download_from_link(link, interaction.id, upload_limit)
            else:
                temp_file = await self.bot.save_attachment(image, interaction.id, upload_limit)

            cleanup_paths.append(temp_file)

            magic_type = MediaProcessor.get_magic_type(temp_file)
            if magic_type != 'unknown' and temp_file.suffix.lower() != f'.{magic_type}':
                proper_temp_file = temp_file.with_name(f"temp_{interaction.id}.{magic_type}")
                try:
                    shutil.move(str(temp_file), str(proper_temp_file))
                    temp_file = proper_temp_file
                    cleanup_paths.append(proper_temp_file)
                except OSError as e:
                    logger.warning(f"Failed to rename temp file: {e}")

            try:
                final_file = await asyncio.to_thread(MediaProcessor.convert_to_gif, temp_file)
            except Exception as e:
                await safe_reply(f"❌ **Conversion Error:** `{e}`")
                return

            if final_file != temp_file:
                cleanup_paths.append(final_file)

            file_size = os.path.getsize(final_file)
            if file_size > upload_limit:
                if file_size <= upload_limit * OVERSIZE_FACTOR:
                    compressed_file = await asyncio.to_thread(MediaProcessor.compress_gif, final_file, upload_limit)

                    if compressed_file != final_file:
                        cleanup_paths.append(compressed_file)
                        final_file = compressed_file

                    file_size = os.path.getsize(final_file)

                if file_size > upload_limit:
                    await safe_reply(
                        f"❌ **Converted File Too Large:** Even after optimization, the GIF ({file_size / (1024 * 1024):.1f} MB) "
                        f"exceeds the server's {upload_limit / (1024 * 1024):.1f} MB limit."
                    )
                    return

            try:
                with open(final_file, 'rb') as f:
                    discord_file = discord.File(f, filename=upload_filename)
                    source_text = f"<{link}>" if link else f"uploaded image (`{image.filename}`)"
                    msg = await channel.send(content=f"Archived from: {source_text}", file=discord_file)

                async with self.bot._index_lock:
                    for att in msg.attachments:
                        self.bot.archive_index.append({
                            "message_id": msg.id,
                            "filename": att.filename,
                            "size": att.size,
                        })

                await safe_reply(f"✅ **Saved successfully!**\n[Click here to jump to the GIF]({msg.jump_url})")

            except discord.errors.HTTPException as e:
                if e.status == 413 or e.code == 40005:
                    await safe_reply(
                        f"❌ **Upload Failed:** Discord rejected the file (Payload Too Large). "
                        f"Size: {file_size / (1024*1024):.1f} MB."
                    )
                else:
                    await safe_reply(f"❌ **Discord API Error:** `{e}`")

        except ValueError as ve:
            await safe_reply(f"❌ {str(ve)}")

        except Exception as e:
            await safe_reply(f"❌ An unexpected error occurred: `{str(e)}`")
            source_log = link if link else image.filename
            logger.error(f"Error archiving {source_log}: {e}", exc_info=True)

        finally:
            for p in cleanup_paths:
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass

    @app_commands.command(name="search", description="Search your GIF archive by filename.")
    @app_commands.describe(query="Search term to match against GIF filenames")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def search_command(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        # --- Input validation ---
        try:
            query = InputValidator.validate_query(query)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}")
            return

        try:
            if not self.bot.index_ready.is_set():
                await interaction.followup.send("⏳ Archive index is still loading. Please try again shortly.")
                return

            query_lower = query.lower()
            search_terms = query_lower.split()

            async with self.bot._index_lock:
                matched_entries = []
                for entry in self.bot.archive_index:
                    name_lower = entry["filename"].lower()
                    if all(term in name_lower for term in search_terms):
                        matched_entries.append(entry)
                    if len(matched_entries) >= MAX_SEARCH_RESULTS:
                        break

            if not matched_entries:
                await interaction.followup.send(f"No GIFs found matching **{query}**.")
                return

            channel = await self.bot.fetch_target_channel()
            matches = []
            stale_ids: set[int] = set()

            for entry in matched_entries:
                try:
                    msg = await channel.fetch_message(entry["message_id"])
                    for att in msg.attachments:
                        if att.filename == entry["filename"]:
                            matches.append({
                                "filename": att.filename,
                                "url": att.url,
                                "jump_url": msg.jump_url,
                                "size": att.size,
                            })
                            break
                except discord.NotFound:
                    stale_ids.add(entry["message_id"])
                except Exception:
                    continue

            if stale_ids:
                async with self.bot._index_lock:
                    self.bot.archive_index = [
                        e for e in self.bot.archive_index if e["message_id"] not in stale_ids
                    ]

            if not matches:
                await interaction.followup.send(f"No accessible GIFs found matching **{query}**.")
                return

            header = f"Found **{len(matches)}** result{'s' if len(matches) != 1 else ''} for **{query}**:"
            embeds = []
            for i, m in enumerate(matches, 1):
                embed = discord.Embed(
                    title=f"{i}. {m['filename']}",
                    url=m["jump_url"],
                    description=f"{m['size'] / 1024:.0f} KB",
                )
                embed.set_image(url=m["url"])
                embeds.append(embed)

            await interaction.followup.send(content=header, embeds=embeds)
            logger.info(f"Search '{query}' by {interaction.user}: {len(matches)} results")

        except Exception as e:
            await interaction.followup.send(f"An error occurred while searching: `{e}`")
            logger.error(f"Search error for query '{query}': {e}", exc_info=True)


def initialize_app():
    parser = argparse.ArgumentParser(description="Discord Archive Bot")
    parser.add_argument("--config", required=True, help="Path to the configuration JSON file")
    args = parser.parse_args()

    DependencyValidator.verify_system_requirements()
    config = AppConfig.load_from_file(args.config)

    bot = ArchiverBot(config)
    return bot, config


if __name__ == "__main__":
    setup_logging()
    app_bot, app_config = initialize_app()
    app_bot.run(app_config.bot_token)