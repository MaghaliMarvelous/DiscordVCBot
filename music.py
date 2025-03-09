import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import asyncio
import re
from urllib.parse import quote
import async_timeout

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
    'extract_flat': 'in_playlist',
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
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',  # Add this to improve stream stability
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.artist = self._extract_artist(data)
    
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
        
        # Use async timeout to prevent hanging
        try:
            async with async_timeout.timeout(30):  # 30 second timeout
                # Use run_in_executor to prevent blocking the event loop
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
                
                if data is None:
                    raise Exception("Could not find any matching songs.")
                
                if 'entries' in data:
                    # Take first item from a playlist
                    if not data['entries']:
                        raise Exception("No results found for this query.")
                    data = data['entries'][0]

                filename = data['url'] if stream else ytdl.prepare_filename(data)
                return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
        except asyncio.TimeoutError:
            raise Exception("The search took too long to complete. Please try again.")


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = {}
        self.now_playing = {}
        self.playing_status = {}
        self.play_next_event = {}  # Add an event to synchronize play_next calls

    async def play_next(self, ctx):
        server_id = ctx.guild.id
        
        # Create an event for this server if it doesn't exist
        if server_id not in self.play_next_event:
            self.play_next_event[server_id] = asyncio.Event()
        
        # Set the event to indicate play_next is currently running
        self.play_next_event[server_id].set()
        
        try:
            # Mark server as not playing at the beginning
            self.playing_status[server_id] = False
            
            # Check if voice client still exists and is connected
            if not ctx.voice_client or not ctx.voice_client.is_connected():
                if server_id in self.queue:
                    self.queue[server_id] = []
                if server_id in self.now_playing:
                    del self.now_playing[server_id]
                return
            
            if server_id in self.queue and self.queue[server_id]:
                # Get next song from queue
                next_song = self.queue[server_id].pop(0)
                self.now_playing[server_id] = next_song
                
                # Set playing status to True before playing
                self.playing_status[server_id] = True
                
                # Define what happens when song finishes
                def after_playing(error):
                    if error:
                        print(f"Player error: {error}")
                    
                    # Run the play_next coroutine when this song finishes
                    # Need to use create_task to properly handle errors
                    asyncio.run_coroutine_threadsafe(
                        self.play_next(ctx), self.bot.loop)
                
                # Play the song with the after callback
                if ctx.voice_client and ctx.voice_client.is_connected():
                    ctx.voice_client.play(next_song, after=after_playing)
                    await ctx.send(f"Now playing - {next_song.title} by {next_song.artist}")
                else:
                    self.playing_status[server_id] = False
            else:
                # No more songs in queue
                if server_id in self.now_playing:
                    del self.now_playing[server_id]
                self.playing_status[server_id] = False
                
                # Only disconnect if there's nothing in the queue
                if ctx.voice_client and ctx.voice_client.is_connected():
                    await ctx.send("Queue is empty. Disconnecting...")
                    await ctx.voice_client.disconnect()
        except Exception as e:
            print(f"Error in play_next: {e}")
            self.playing_status[server_id] = False
        finally:
            # Clear the event to indicate play_next is no longer running
            self.play_next_event[server_id].clear()

    async def search_youtube_music(self, query):
        """Search on YouTube Music specifically"""
        # Format the query to specifically target YouTube Music
        # Add "audio" to prioritize music over videos
        formatted_query = f"{query} audio"
        search_url = f"ytsearch:{formatted_query}"
        
        print(f"Searching for: {search_url}")
        
        # Extract info with a timeout to prevent hanging
        try:
            loop = asyncio.get_event_loop()
            async with async_timeout.timeout(30):
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(
                    search_url, download=False, process=False))
                
                if not data or not data.get('entries'):
                    raise Exception("No results found for this query.")
                
                # Filter results to prioritize music content
                entries = data['entries']
                
                # Find the first entry that looks like a music track
                # This is a simple heuristic that can be improved
                for entry in entries:
                    video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                    # Get full info for this specific video
                    full_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(
                        video_url, download=False))
                    
                    return full_data
                
                # If no good match found, just use the first result
                if entries:
                    video_url = f"https://www.youtube.com/watch?v={entries[0]['id']}"
                    return await loop.run_in_executor(None, lambda: ytdl.extract_info(
                        video_url, download=False))
                
                raise Exception("Could not find any suitable music tracks.")
        except asyncio.TimeoutError:
            raise Exception("The search took too long to complete. Please try again.")
        except Exception as e:
            print(f"Error searching YouTube Music: {e}")
            raise Exception(f"Error searching: {str(e)}")

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
            channel = ctx.author.voice.channel
            await channel.connect()

        await ctx.send(f"🔍 Searching for: {song_query}")
        
        async with ctx.typing():
            try:
                # Search for song on YouTube Music
                print(f"Searching for: {song_query}")
                data = await self.search_youtube_music(song_query)
                
                # Create a player from the found video
                player = YTDLSource(discord.FFmpegPCMAudio(
                    data['url'], **ffmpeg_options), data=data)
                
                print(f"Found song: {player.title}")
                
                server_id = ctx.guild.id
                
                # Initialize queue and status for this server if they don't exist
                if server_id not in self.queue:
                    self.queue[server_id] = []
                if server_id not in self.playing_status:
                    self.playing_status[server_id] = False
                
                # Check if something is already playing
                is_playing = self.playing_status.get(server_id, False)
                if ctx.voice_client:
                    is_playing = is_playing or ctx.voice_client.is_playing()
                
                # If something is already playing, add to queue
                if is_playing:
                    self.queue[server_id].append(player)
                    await ctx.send(f"Added to queue: {player.title} by {player.artist}")
                else:
                    # Otherwise play immediately
                    self.queue[server_id].append(player)
                    
                    # Check if play_next is already running
                    if server_id in self.play_next_event and self.play_next_event[server_id].is_set():
                        # If so, just wait for it to finish - the song is already in the queue
                        pass
                    else:
                        # Otherwise start playing
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
            channel = ctx.author.voice.channel
            await channel.connect()

        await ctx.send(f"🔍 Searching for: {song_query}")
        
        async with ctx.typing():
            try:
                # Search for song on YouTube Music
                data = await self.search_youtube_music(song_query)
                
                # Create a player from the found video
                player = YTDLSource(discord.FFmpegPCMAudio(
                    data['url'], **ffmpeg_options), data=data)
                
                server_id = ctx.guild.id
                
                # Initialize queue and status for this server if they don't exist
                if server_id not in self.queue:
                    self.queue[server_id] = []
                if server_id not in self.playing_status:
                    self.playing_status[server_id] = False
                
                # Add to queue
                self.queue[server_id].append(player)
                await ctx.send(f"Added to queue: {player.title} by {player.artist}")
                
                # Check if something is playing
                is_playing = self.playing_status.get(server_id, False)
                if ctx.voice_client:
                    is_playing = is_playing or ctx.voice_client.is_playing()
                
                # Only start playing if nothing is currently playing
                if not is_playing:
                    # Check if play_next is already running
                    if server_id in self.play_next_event and self.play_next_event[server_id].is_set():
                        # If so, just wait for it to finish - the song is already in the queue
                        pass
                    else:
                        # Otherwise start playing
                        await self.play_next(ctx)
            except Exception as e:
                await ctx.send(f"An error occurred: {str(e)}")
                print(f"Error in add command: {e}")

    @commands.command(name='stop')
    async def stop(self, ctx):
        if ctx.voice_client:
            server_id = ctx.guild.id
            # Clear queue
            if server_id in self.queue:
                self.queue[server_id] = []
            if server_id in self.now_playing:
                del self.now_playing[server_id]
            
            # Update playing status before disconnecting
            self.playing_status[server_id] = False
            
            # Stop playing and disconnect
            ctx.voice_client.stop()
            await ctx.voice_client.disconnect()
            await ctx.send("Music stopped and queue cleared.")

    @commands.command(name='skip')
    async def skip(self, ctx):
        server_id = ctx.guild.id
        # Check if something is playing using our improved status check
        is_playing = self.playing_status.get(server_id, False)
        if ctx.voice_client:
            is_playing = is_playing or ctx.voice_client.is_playing()
            
        if is_playing:
            # Update playing status before stopping
            self.playing_status[server_id] = False
            ctx.voice_client.stop()  # This will trigger the after function and play the next song
            await ctx.send("Skipped current song.")
        else:
            await ctx.send("Nothing is playing right now.")

    @commands.command(name='queue')
    async def queue_list(self, ctx):
        server_id = ctx.guild.id
        if server_id not in self.queue or not self.queue[server_id]:
            await ctx.send("The queue is empty.")
            return
            
        queue_list = "\n".join([f"{i+1}. {song.title} by {song.artist}" 
                              for i, song in enumerate(self.queue[server_id])])
        await ctx.send(f"**Current queue:**\n{queue_list}")

    @commands.command(name='nowplaying')
    async def now_playing_cmd(self, ctx):
        server_id = ctx.guild.id
        # Check if something is playing using our improved status check
        is_playing = self.playing_status.get(server_id, False)
        if ctx.voice_client:
            is_playing = is_playing or ctx.voice_client.is_playing()
            
        if server_id in self.now_playing and is_playing:
            current = self.now_playing[server_id]
            await ctx.send(f"**Now Playing:** {current.title} by {current.artist}")
        else:
            await ctx.send("Nothing is playing right now.")

    # Add a command to force disconnect and reset state
    @commands.command(name='reset')
    async def reset(self, ctx):
        server_id = ctx.guild.id
        
        # Clear all state for this server
        if server_id in self.queue:
            self.queue[server_id] = []
        if server_id in self.now_playing:
            del self.now_playing[server_id]
        self.playing_status[server_id] = False
        
        # Force disconnect
        if ctx.voice_client and ctx.voice_client.is_connected():
            ctx.voice_client.stop()
            await ctx.voice_client.disconnect()
            
        await ctx.send("Bot state reset. All queues cleared and disconnected.")

@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    await bot.add_cog(Music(bot))

# Replace with your actual bot token
bot.run('BOT TOKEN KAMU')
