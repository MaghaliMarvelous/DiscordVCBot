import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import asyncio
import re
from urllib.parse import quote

# Set up bot with command prefix
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# YouTube DL options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # Add these options to fix audio issues
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    # Force using all audio channels
    'postprocessor_args': [
        '-ar', '48000',
        '-ac', '2',  # Stereo audio
        '-vn'
    ],
}

ffmpeg_options = {
    'options': '-vn',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0 -loglevel 0'  # Improved reconnect options
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.artist = self._extract_artist(data)
        self.duration = data.get('duration', 0)  # Track duration
        self.is_live = data.get('is_live', False)  # Check if livestream
    
    @staticmethod
    def _extract_artist(data):
        # Try to extract artist from video title or uploader
        title = data.get('title', '')
        uploader = data.get('uploader', '')
        
        # Look for "Song - Artist" or "Artist - Song" format in title
        match = re.search(r'(.+?)\s*[-–]\s*(.+)', title)
        if match:
            # Assuming most YouTube music videos format is "Artist - Song"
            part1, part2 = match.groups()
            return part1.strip()  # Assume first part is artist
        
        # If no match in title, use uploader as fallback
        return uploader

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
            
            if 'entries' in data:
                # Take first item from a playlist
                data = data['entries'][0]

            filename = data['url'] if stream else ytdl.prepare_filename(data)
            return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
        except Exception as e:
            print(f"Error extracting info: {e}")
            raise e


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = {}
        self.now_playing = {}
        self.playing_status = {}
        self.message_tasks = {}  # To track message tasks
        self.skip_requested = {}  # Track if skip was requested
        self.lock = {}  # Locks for each server to prevent race conditions

    async def get_lock(self, server_id):
        """Get or create a lock for a server"""
        if server_id not in self.lock:
            self.lock[server_id] = asyncio.Lock()
        return self.lock[server_id]

    async def delayed_send(self, ctx, message, delay=0.5):
        """Send a message with a delay to prevent voice lag"""
        await asyncio.sleep(delay)
        try:
            await ctx.send(message)
        except discord.errors.DiscordException as e:
            print(f"Error sending message: {e}")

    async def play_next(self, ctx, error=None):
        server_id = ctx.guild.id
        lock = await self.get_lock(server_id)
        
        # Use lock to prevent race conditions
        async with lock:
            # Check if skip was requested
            skip_requested = self.skip_requested.get(server_id, False)
            self.skip_requested[server_id] = False  # Reset skip flag
            
            # Log any errors that caused the previous song to end
            if error and not skip_requested:
                print(f"Player error caused song to end: {error}")
                try:
                    await ctx.send(f"Error playing song: {error}. Trying next song...")
                except:
                    pass
            
            # Mark server as not playing at the beginning
            self.playing_status[server_id] = False
            
            if server_id in self.queue and self.queue[server_id]:
                # Get next song from queue
                next_song = self.queue[server_id].pop(0)
                self.now_playing[server_id] = next_song
                
                # Define the after callback (more robust now)
                def after_playing(e):
                    # This will run in a different thread, so we need to create a new task
                    coro = self.play_next(ctx, error=e)
                    fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                    
                    # Add error handling to the future
                    def handle_future_error(future):
                        try:
                            future.result()
                        except Exception as e:
                            print(f"Error in play_next future: {e}")
                    
                    fut.add_done_callback(handle_future_error)
                
                # Set playing status to True before playing
                self.playing_status[server_id] = True
                
                # Check if voice client is still connected
                if not ctx.voice_client or not ctx.voice_client.is_connected():
                    try:
                        # Try to reconnect
                        if ctx.author.voice:
                            channel = ctx.author.voice.channel
                            await channel.connect()
                            await asyncio.sleep(0.5)
                        else:
                            await self.delayed_send(ctx, "Voice channel no longer available.")
                            self.playing_status[server_id] = False
                            return
                    except discord.errors.ClientException:
                        await self.delayed_send(ctx, "Could not connect to voice channel.")
                        self.playing_status[server_id] = False
                        return
                
                # Start playing the song
                try:
                    ctx.voice_client.play(next_song, after=after_playing)
                    
                    # Send the message with a slight delay
                    duration_str = ""
                    if next_song.duration > 0 and not next_song.is_live:
                        minutes, seconds = divmod(next_song.duration, 60)
                        duration_str = f" [{minutes}:{seconds:02d}]"
                    
                    message = f"Now playing - {next_song.title} by {next_song.artist}{duration_str}"
                    task = asyncio.create_task(self.delayed_send(ctx, message))
                    self.message_tasks[server_id] = task
                    
                except Exception as e:
                    await self.delayed_send(ctx, f"Error starting playback: {str(e)}")
                    print(f"Playback error: {e}")
                    # Try the next song
                    self.playing_status[server_id] = False
                    await self.play_next(ctx)
                
            else:
                # No more songs in queue
                if server_id in self.now_playing:
                    del self.now_playing[server_id]
                if server_id in self.playing_status:
                    del self.playing_status[server_id]
                    
                # Send disconnect message with delay and then disconnect
                if ctx.voice_client and ctx.voice_client.is_connected():
                    task = asyncio.create_task(self.delayed_send(ctx, "Queue finished. Disconnecting..."))
                    self.message_tasks[server_id] = task
                    # Wait before disconnecting
                    await asyncio.sleep(1.5)
                    try:
                        await ctx.voice_client.disconnect()
                    except:
                        pass

    @commands.command(name='sing')
    async def sing(self, ctx, *, song_query=None):
        if not song_query:
            await ctx.send("Please provide a song name after !sing")
            return

        # Check if user is in a voice channel
        if not ctx.author.voice:
            await ctx.send("You need to be in a voice channel to use this command.")
            return

        # Connect to voice channel if not already connected
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            try:
                channel = ctx.author.voice.channel
                await channel.connect()
                # Brief pause after connecting to prevent lag
                await asyncio.sleep(0.5)
            except discord.errors.ClientException as e:
                await ctx.send(f"Could not connect to voice channel: {str(e)}")
                return

        # Search for song on YouTube
        search_query = quote(song_query)
        url = f"ytsearch:{search_query}"
        
        async with ctx.typing():
            try:
                # Let the user know we're searching
                await ctx.send("Searching for your song...")
                
                # Get the song
                player = await YTDLSource.from_url(url, loop=self.bot.loop, stream=True)
                server_id = ctx.guild.id
                
                # Initialize queue and status for this server if they don't exist
                if server_id not in self.queue:
                    self.queue[server_id] = []
                if server_id not in self.playing_status:
                    self.playing_status[server_id] = False
                if server_id not in self.skip_requested:
                    self.skip_requested[server_id] = False
                
                # Add to queue first
                self.queue[server_id].append(player)
                
                # Get the lock for this server
                lock = await self.get_lock(server_id)
                
                async with lock:
                    # Check if something is already playing
                    server_is_playing = self.playing_status.get(server_id, False) or (ctx.voice_client and ctx.voice_client.is_playing())
                    
                    if server_is_playing:
                        await self.delayed_send(ctx, f"Added to queue: {player.title} by {player.artist}")
                    else:
                        # Play immediately
                        await self.play_next(ctx)
            except Exception as e:
                await ctx.send(f"An error occurred: {str(e)}")
                print(f"Error in sing command: {e}")

    @commands.command(name='add')
    async def add_to_queue(self, ctx, *, song_query=None):
        if not song_query:
            await ctx.send("Please provide a song name after !add")
            return

        # Check if user is in a voice channel
        if not ctx.author.voice:
            await ctx.send("You need to be in a voice channel to use this command.")
            return

        # Connect to voice channel if not already connected
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            try:
                channel = ctx.author.voice.channel
                await channel.connect()
                # Brief pause after connecting to prevent lag
                await asyncio.sleep(0.5)
            except discord.errors.ClientException as e:
                await ctx.send(f"Could not connect to voice channel: {str(e)}")
                return

        # Search for song on YouTube
        search_query = quote(song_query)
        url = f"ytsearch:{search_query}"
        
        async with ctx.typing():
            try:
                # Let the user know we're searching
                await ctx.send("Searching for your song...")
                
                # Get the song
                player = await YTDLSource.from_url(url, loop=self.bot.loop, stream=True)
                server_id = ctx.guild.id
                
                # Initialize queue and status for this server if they don't exist
                if server_id not in self.queue:
                    self.queue[server_id] = []
                if server_id not in self.playing_status:
                    self.playing_status[server_id] = False
                if server_id not in self.skip_requested:
                    self.skip_requested[server_id] = False
                
                # Add to queue
                self.queue[server_id].append(player)
                await self.delayed_send(ctx, f"Added to queue: {player.title} by {player.artist}")
                
                # Get the lock for this server
                lock = await self.get_lock(server_id)
                
                async with lock:
                    # Check if something is playing
                    server_is_playing = self.playing_status.get(server_id, False) or (ctx.voice_client and ctx.voice_client.is_playing())
                    
                    # Only start playing if nothing is currently playing
                    if not server_is_playing:
                        await self.play_next(ctx)
            except Exception as e:
                await ctx.send(f"An error occurred: {str(e)}")
                print(f"Error in add command: {e}")

    @commands.command(name='stop')
    async def stop(self, ctx):
        server_id = ctx.guild.id
        # Get lock
        lock = await self.get_lock(server_id)
        
        async with lock:
            if ctx.voice_client:
                # Clear queue
                if server_id in self.queue:
                    self.queue[server_id] = []
                if server_id in self.now_playing:
                    del self.now_playing[server_id]
                if server_id in self.playing_status:
                    self.playing_status[server_id] = False
                if server_id in self.skip_requested:
                    self.skip_requested[server_id] = False
                
                # Cancel any pending message tasks
                if server_id in self.message_tasks and not self.message_tasks[server_id].done():
                    self.message_tasks[server_id].cancel()
                
                # Stop playback
                if ctx.voice_client.is_playing():
                    ctx.voice_client.stop()
                
                await ctx.send("Music stopped. Disconnecting...")
                # Wait briefly before disconnecting
                await asyncio.sleep(0.5)
                await ctx.voice_client.disconnect()
            else:
                await ctx.send("Not connected to a voice channel.")

    @commands.command(name='skip')
    async def skip(self, ctx):
        server_id = ctx.guild.id
        lock = await self.get_lock(server_id)
        
        async with lock:
            if ctx.voice_client and (ctx.voice_client.is_playing() or self.playing_status.get(server_id, False)):
                await ctx.send("Skipping current song...")
                # Mark skip as requested to avoid error messages
                self.skip_requested[server_id] = True
                
                # Cancel any pending message tasks
                if server_id in self.message_tasks and not self.message_tasks[server_id].done():
                    self.message_tasks[server_id].cancel()
                
                # Stop current song (this will trigger play_next)
                ctx.voice_client.stop()
            else:
                await ctx.send("Nothing is playing right now.")

    @commands.command(name='queue')
    async def queue_list(self, ctx):
        server_id = ctx.guild.id
        
        # Show currently playing song
        current_song = ""
        if server_id in self.now_playing and self.playing_status.get(server_id, False):
            current = self.now_playing[server_id]
            current_song = f"**Now Playing:** {current.title} by {current.artist}\n\n"
        
        # Show queue
        if server_id not in self.queue or not self.queue[server_id]:
            if current_song:
                await ctx.send(f"{current_song}**Queue is empty.**")
            else:
                await ctx.send("The queue is empty.")
            return
            
        queue_list = "\n".join([f"{i+1}. {song.title} by {song.artist}" 
                              for i, song in enumerate(self.queue[server_id])])
        
        await ctx.send(f"{current_song}**Current queue:**\n{queue_list}")

    @commands.command(name='nowplaying')
    async def now_playing_cmd(self, ctx):
        server_id = ctx.guild.id
        if server_id in self.now_playing and self.playing_status.get(server_id, False):
            current = self.now_playing[server_id]
            # Add duration if available
            duration_str = ""
            if hasattr(current, 'duration') and current.duration > 0 and not current.is_live:
                minutes, seconds = divmod(current.duration, 60)
                duration_str = f" [{minutes}:{seconds:02d}]"
                
            await ctx.send(f"**Now Playing:** {current.title} by {current.artist}{duration_str}")
        else:
            await ctx.send("Nothing is playing right now.")
            
    @commands.command(name='ping')
    async def ping(self, ctx):
        """Check if the bot is responsive"""
        await ctx.send(f"Pong! Bot latency: {round(self.bot.latency * 1000)}ms")

@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    await bot.add_cog(Music(bot))
    
    # Set custom status
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="!sing commands"))

# Replace with your actual Discord bot token
bot.run('BOT TOKEN KAMU')
