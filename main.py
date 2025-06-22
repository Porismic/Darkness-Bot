
import discord
from discord.ext import commands
from discord import app_commands
import json
import asyncio
import time
from typing import Optional, List
from datetime import datetime, timedelta
import pytz
import aiohttp
import os
import io
from PIL import Image
import random

# Bot configuration
ALLOWED_GUILD_IDS = []  # Configure this list with guild IDs to restrict bot usage
BOT_TOKEN = None  # Will be loaded from environment variable

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.invites = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Data storage
autoresponders = {}
auction_settings = {
    'channel_id': None,
    'format': 'thread',  # 'thread', 'channel', 'forum'
    'forum_channel_id': None
}

# Enhanced bot configuration with default Administrator requirements
bot_config = {
    'command_permissions': {
        'config': [],
        'autoresponder': [],
        'autoresponders': [],
        'auctionsetup': [],
        'auctioncreate': [],
        'embedcreator': [],
        'reactionroles': [],
        'boostsetup': [],
        'invitesetup': [],
        'invites': [],
        'connect4': [],
        'endgame': [],
        'test_autoresponder': [],
        'export_autoresponders': []
    },  # command_name: [role_ids] - empty means Administrator required
    'admin_roles': [],
    'moderator_roles': []
}

boost_settings = {
    'roles': {},  # boost_count: role_id
    'tracking': {},  # user_id: {'boosts': count, 'boost_history': [], 'current_boost_start': timestamp}
    'guild_boost_count': {}  # guild_id: total_boosts (for comparison)
}

invite_settings = {
    'roles': {},  # invite_count: role_id
    'tracking': {},  # user_id: {'invites': count, 'invited_users': []}
    'invite_cache': {}  # invite_code: uses
}

embed_storage = {}  # message_id: embed_data
reaction_roles = {}  # message_id: {emoji: role_id}

# Connect 4 game storage
active_games = {}  # channel_id: game_data

class Connect4Game:
    def __init__(self, player1, player2, channel):
        self.player1 = player1
        self.player2 = player2
        self.current_player = player1
        self.channel = channel
        self.board = [[0 for _ in range(7)] for _ in range(6)]  # 6 rows, 7 columns
        self.landmines = self.generate_landmines()
        self.turns_lost = {player1.id: 0, player2.id: 0}
        self.game_over = False
        self.winner = None
        
    def generate_landmines(self):
        """Generate 3-5 random landmine positions"""
        landmines = set()
        num_mines = random.randint(3, 5)
        while len(landmines) < num_mines:
            row = random.randint(0, 5)
            col = random.randint(0, 6)
            landmines.add((row, col))
        return landmines
    
    def make_move(self, column):
        """Make a move in the specified column"""
        if self.game_over:
            return {"valid": False, "reason": "Game is over"}
        
        if column < 0 or column > 6:
            return {"valid": False, "reason": "Invalid column"}
        
        # Find the lowest empty row in the column
        for row in range(5, -1, -1):
            if self.board[row][column] == 0:
                # Check for landmine
                if (row, column) in self.landmines:
                    self.turns_lost[self.current_player.id] += 2
                    return {"valid": True, "landmine": True, "position": (row, column)}
                
                # Place the piece
                player_num = 1 if self.current_player == self.player1 else 2
                self.board[row][column] = player_num
                
                # Check for win
                if self.check_win(row, column, player_num):
                    self.game_over = True
                    self.winner = self.current_player
                
                return {"valid": True, "landmine": False, "position": (row, column)}
        
        return {"valid": False, "reason": "Column is full"}
    
    def check_win(self, row, col, player_num):
        """Check if the current move results in a win"""
        directions = [
            (0, 1),   # horizontal
            (1, 0),   # vertical
            (1, 1),   # diagonal /
            (1, -1)   # diagonal \
        ]
        
        for dr, dc in directions:
            count = 1  # Count the current piece
            
            # Check in positive direction
            r, c = row + dr, col + dc
            while 0 <= r < 6 and 0 <= c < 7 and self.board[r][c] == player_num:
                count += 1
                r, c = r + dr, c + dc
            
            # Check in negative direction
            r, c = row - dr, col - dc
            while 0 <= r < 6 and 0 <= c < 7 and self.board[r][c] == player_num:
                count += 1
                r, c = r - dr, c - dc
            
            if count >= 4:
                return True
        
        return False
    
    def get_board_display(self):
        """Get a visual representation of the board"""
        display = "```\n"
        display += "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣\n"
        
        for row in self.board:
            for cell in row:
                if cell == 0:
                    display += "⚫"
                elif cell == 1:
                    display += "🔴"
                else:
                    display += "🔵"
            display += "\n"
        
        display += "```"
        return display
    
    def next_turn(self):
        """Switch to the next player"""
        if self.turns_lost[self.current_player.id] > 0:
            self.turns_lost[self.current_player.id] -= 1
            return f"{self.current_player.mention} loses a turn! ({self.turns_lost[self.current_player.id]} turns remaining to lose)"
        
        self.current_player = self.player2 if self.current_player == self.player1 else self.player1
        return None

def has_permission(user_roles, command_name, user):
    """Check if user has permission to use a command"""
    # Admin roles bypass all restrictions
    if bot_config['admin_roles']:
        user_role_ids = [role.id for role in user_roles]
        if any(role_id in user_role_ids for role_id in bot_config['admin_roles']):
            return True
    
    # Check if user has Administrator permission (default requirement)
    if user.guild_permissions.administrator:
        return True
    
    # Check command-specific permissions
    if command_name in bot_config['command_permissions']:
        allowed_roles = bot_config['command_permissions'][command_name]
        if not allowed_roles:
            # Empty list means Administrator permission required
            return False
        
        user_role_ids = [role.id for role in user_roles]
        return any(role_id in user_role_ids for role_id in allowed_roles)
    
    # Default: require Administrator
    return False

def guild_only():
    """Decorator to restrict commands to allowed guilds"""
    def predicate(interaction: discord.Interaction) -> bool:
        if not ALLOWED_GUILD_IDS:
            return True  # No guild restriction set
        return interaction.guild_id in ALLOWED_GUILD_IDS
    return app_commands.check(predicate)

async def download_image(url, max_size=8*1024*1024):
    """Download and validate image"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                
                content_length = response.headers.get('content-length')
                if content_length and int(content_length) > max_size:
                    return None
                
                data = await response.read()
                if len(data) > max_size:
                    return None
                
                # Validate it's an image
                try:
                    Image.open(io.BytesIO(data))
                    return data
                except:
                    return None
    except:
        return None

class Connect4View(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=300)
        self.game = game
        
        # Add column buttons
        for i in range(7):
            self.add_item(Connect4Button(i + 1, i))
        
        # Add game control buttons
        self.add_item(EndGameButton())
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow the current player to make moves
        if self.game.game_over:
            return True
        return interaction.user == self.game.current_player

class Connect4Button(discord.ui.Button):
    def __init__(self, label, column):
        super().__init__(style=discord.ButtonStyle.secondary, label=str(label), row=0)
        self.column = column
    
    async def callback(self, interaction: discord.Interaction):
        game = self.view.game
        
        if game.game_over:
            await interaction.response.send_message("Game is already over!", ephemeral=True)
            return
        
        if interaction.user != game.current_player:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return
        
        result = game.make_move(self.column)
        
        if not result["valid"]:
            await interaction.response.send_message(result["reason"], ephemeral=True)
            return
        
        # Handle landmine
        if result["landmine"]:
            embed = discord.Embed(
                title="💥 LANDMINE EXPLOSION!",
                description=f"{game.current_player.mention} hit a landmine and loses 2 turns!",
                color=0xff0000
            )
            embed.add_field(name="Board", value=game.get_board_display(), inline=False)
            embed.add_field(name="Next Turn", value=f"{game.current_player.mention} (turns to lose: {game.turns_lost[game.current_player.id]})", inline=False)
            
            await interaction.response.edit_message(embed=embed, view=self.view)
            return
        
        # Check for win
        if game.winner:
            embed = discord.Embed(
                title="🎉 Game Over!",
                description=f"**{game.winner.mention}** wins the game!",
                color=0x00ff00
            )
            embed.add_field(name="Final Board", value=game.get_board_display(), inline=False)
            
            # Remove the game from active games
            if game.channel.id in active_games:
                del active_games[game.channel.id]
            
            # Disable all buttons
            for item in self.view.children:
                item.disabled = True
            
            await interaction.response.edit_message(embed=embed, view=self.view)
            return
        
        # Continue game
        turn_message = game.next_turn()
        
        embed = discord.Embed(
            title="Connect 4 with Landmines",
            description=f"Current turn: {game.current_player.mention}",
            color=0x0099ff
        )
        embed.add_field(name="Board", value=game.get_board_display(), inline=False)
        
        if turn_message:
            embed.add_field(name="Turn Lost", value=turn_message, inline=False)
        
        embed.set_footer(text="Click a column number to drop your piece!")
        
        await interaction.response.edit_message(embed=embed, view=self.view)

class EndGameButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.danger, label="End Game", row=1)
    
    async def callback(self, interaction: discord.Interaction):
        game = self.view.game
        
        # Only players can end the game
        if interaction.user not in [game.player1, game.player2]:
            await interaction.response.send_message("Only players can end the game!", ephemeral=True)
            return
        
        game.game_over = True
        
        if game.channel.id in active_games:
            del active_games[game.channel.id]
        
        embed = discord.Embed(
            title="Game Ended",
            description=f"Game ended by {interaction.user.mention}",
            color=0xff0000
        )
        embed.add_field(name="Final Board", value=game.get_board_display(), inline=False)
        
        # Disable all buttons
        for item in self.view.children:
            item.disabled = True
        
        await interaction.response.edit_message(embed=embed, view=self.view)

class ConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Command Permissions", style=discord.ButtonStyle.blurple, emoji="🔐")
    async def command_permissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CommandPermissionsModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Admin Roles", style=discord.ButtonStyle.green, emoji="👑")
    async def admin_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AdminRolesModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Guild Settings", style=discord.ButtonStyle.secondary, emoji="🏠")
    async def guild_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = GuildSettingsModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="View Config", style=discord.ButtonStyle.gray, emoji="📋")
    async def view_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Bot Configuration", color=0x0099ff)
        
        # Guild restriction info
        if ALLOWED_GUILD_IDS:
            guild_names = []
            for guild_id in ALLOWED_GUILD_IDS:
                guild = bot.get_guild(guild_id)
                guild_names.append(guild.name if guild else f"Guild ID: {guild_id}")
            embed.add_field(name="Allowed Guilds", value=", ".join(guild_names), inline=False)
        else:
            embed.add_field(name="Guild Restriction", value="Not restricted (all servers)", inline=False)
        
        if bot_config['admin_roles']:
            admin_roles = [f"<@&{role_id}>" for role_id in bot_config['admin_roles']]
            embed.add_field(name="Admin Roles", value=", ".join(admin_roles), inline=False)
        
        # List all available commands
        all_commands = list(bot_config['command_permissions'].keys())
        
        perms_text = ""
        for cmd in all_commands:
            roles = bot_config['command_permissions'].get(cmd, [])
            if roles:
                role_mentions = [f"<@&{role_id}>" for role_id in roles]
                perms_text += f"**{cmd}:** {', '.join(role_mentions)}\n"
            else:
                perms_text += f"**{cmd}:** Administrator required\n"
        
        if perms_text:
            embed.add_field(name="Command Permissions", value=perms_text, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class CommandPermissionsModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Command Permissions")
    
    command = discord.ui.TextInput(
        label="Command Name",
        placeholder="Available commands: " + ", ".join(list(bot_config['command_permissions'].keys())),
        max_length=50
    )
    
    roles = discord.ui.TextInput(
        label="Allowed Role IDs (comma-separated)",
        placeholder="Enter role IDs or leave empty to require Administrator",
        required=False,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # Validate command name
        valid_commands = list(bot_config['command_permissions'].keys())
        
        if self.command.value not in valid_commands:
            await interaction.response.send_message(f"Invalid command. Available commands: {', '.join(valid_commands)}", ephemeral=True)
            return
        
        role_ids = []
        if self.roles.value:
            try:
                role_ids = [int(role.strip()) for role in self.roles.value.split(',') if role.strip()]
                # Validate that roles exist in the guild
                invalid_roles = []
                for role_id in role_ids:
                    role = interaction.guild.get_role(role_id)
                    if not role:
                        invalid_roles.append(str(role_id))
                
                if invalid_roles:
                    await interaction.response.send_message(f"Invalid role IDs: {', '.join(invalid_roles)}", ephemeral=True)
                    return
                        
            except ValueError:
                await interaction.response.send_message("Invalid role ID format.", ephemeral=True)
                return
        
        bot_config['command_permissions'][self.command.value] = role_ids
        
        permission_text = "Administrator required" if not role_ids else f"Roles: {', '.join([f'<@&{r}>' for r in role_ids])}"
        await interaction.response.send_message(f"Permissions updated for **{self.command.value}**: {permission_text}", ephemeral=True)

class AdminRolesModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Admin Roles")
    
    roles = discord.ui.TextInput(
        label="Admin Role IDs (comma-separated)",
        placeholder="Enter role IDs for admin access",
        required=False,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        role_ids = []
        if self.roles.value:
            try:
                role_ids = [int(role.strip()) for role in self.roles.value.split(',') if role.strip()]
                # Validate that roles exist in the guild
                invalid_roles = []
                for role_id in role_ids:
                    role = interaction.guild.get_role(role_id)
                    if not role:
                        invalid_roles.append(str(role_id))
                
                if invalid_roles:
                    await interaction.response.send_message(f"Invalid role IDs: {', '.join(invalid_roles)}", ephemeral=True)
                    return
                        
            except ValueError:
                await interaction.response.send_message("Invalid role ID format.", ephemeral=True)
                return
        
        bot_config['admin_roles'] = role_ids
        await interaction.response.send_message("Admin roles updated successfully!", ephemeral=True)

class GuildSettingsModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Guild Restriction Settings")
    
    guild_ids = discord.ui.TextInput(
        label="Allowed Guild IDs (comma-separated)",
        placeholder="Enter guild IDs to restrict bot, or leave empty for no restriction",
        required=False,
        max_length=200
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        global ALLOWED_GUILD_IDS
        
        if not self.guild_ids.value:
            ALLOWED_GUILD_IDS = []
            await interaction.response.send_message("Guild restriction removed. Bot can be used in any server.", ephemeral=True)
            return
        
        try:
            guild_ids = [int(guild_id.strip()) for guild_id in self.guild_ids.value.split(',') if guild_id.strip()]
            
            # Validate guild IDs
            invalid_guilds = []
            valid_guilds = []
            for guild_id in guild_ids:
                guild = bot.get_guild(guild_id)
                if not guild:
                    invalid_guilds.append(str(guild_id))
                else:
                    valid_guilds.append(guild)
            
            if invalid_guilds:
                await interaction.response.send_message(f"Warning: Bot is not in these guilds: {', '.join(invalid_guilds)}\nMake sure the bot is added to those servers.", ephemeral=True)
                return
            
            ALLOWED_GUILD_IDS = guild_ids
            guild_names = [guild.name for guild in valid_guilds]
            await interaction.response.send_message(f"Bot restricted to guilds: {', '.join(guild_names)}", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("Invalid guild ID format.", ephemeral=True)

class EmbedCreatorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.embed_data = {
            'title': '',
            'description': '',
            'color': 0x0099ff,
            'thumbnail': '',
            'image': '',
            'footer': ''
        }
    
    @discord.ui.button(label="Set Title", style=discord.ButtonStyle.blurple)
    async def set_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EmbedTitleModal(self.embed_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Description", style=discord.ButtonStyle.blurple)
    async def set_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EmbedDescriptionModal(self.embed_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Color", style=discord.ButtonStyle.blurple)
    async def set_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EmbedColorModal(self.embed_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Images", style=discord.ButtonStyle.blurple)
    async def set_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EmbedImagesModal(self.embed_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Preview", style=discord.ButtonStyle.green)
    async def preview_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.create_embed()
        await interaction.response.send_message("**Preview:**", embed=embed, ephemeral=True)
    
    @discord.ui.button(label="Send to Channel", style=discord.ButtonStyle.red)
    async def send_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SendEmbedModal(self.embed_data)
        await interaction.response.send_modal(modal)
    
    def create_embed(self):
        embed = discord.Embed(color=self.embed_data['color'])
        
        if self.embed_data['title']:
            embed.title = self.embed_data['title']
        if self.embed_data['description']:
            embed.description = self.embed_data['description']
        if self.embed_data['thumbnail']:
            embed.set_thumbnail(url=self.embed_data['thumbnail'])
        if self.embed_data['image']:
            embed.set_image(url=self.embed_data['image'])
        if self.embed_data['footer']:
            embed.set_footer(text=self.embed_data['footer'])
        
        return embed

class EmbedTitleModal(discord.ui.Modal):
    def __init__(self, embed_data):
        super().__init__(title="Set Embed Title")
        self.embed_data = embed_data
    
    title = discord.ui.TextInput(
        label="Title",
        default="",
        max_length=256,
        required=False
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.embed_data['title'] = self.title.value
        await interaction.response.send_message("Title updated!", ephemeral=True)

class EmbedDescriptionModal(discord.ui.Modal):
    def __init__(self, embed_data):
        super().__init__(title="Set Embed Description")
        self.embed_data = embed_data
    
    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        default="",
        max_length=4000,
        required=False
    )
    
    footer = discord.ui.TextInput(
        label="Footer (optional)",
        default="",
        max_length=2048,
        required=False
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.embed_data['description'] = self.description.value
        self.embed_data['footer'] = self.footer.value
        await interaction.response.send_message("Description and footer updated!", ephemeral=True)

class EmbedColorModal(discord.ui.Modal):
    def __init__(self, embed_data):
        super().__init__(title="Set Embed Color")
        self.embed_data = embed_data
    
    color = discord.ui.TextInput(
        label="Color (hex code)",
        placeholder="e.g., #FF0000 or FF0000",
        max_length=7
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # Set default value if empty
        if not self.color.value:
            self.color.value = hex(self.embed_data['color'])
        
        try:
            color_value = self.color.value.replace('#', '')
            self.embed_data['color'] = int(color_value, 16)
            await interaction.response.send_message("Color updated!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid hex color format!", ephemeral=True)

class EmbedImagesModal(discord.ui.Modal):
    def __init__(self, embed_data):
        super().__init__(title="Set Embed Images")
        self.embed_data = embed_data
    
    thumbnail = discord.ui.TextInput(
        label="Thumbnail URL",
        default="",
        required=False,
        max_length=500
    )
    
    image = discord.ui.TextInput(
        label="Bottom Image URL",
        default="",
        required=False,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.embed_data['thumbnail'] = self.thumbnail.value
        self.embed_data['image'] = self.image.value
        await interaction.response.send_message("Images updated!", ephemeral=True)

class SendEmbedModal(discord.ui.Modal):
    def __init__(self, embed_data):
        super().__init__(title="Send Embed to Channel")
        self.embed_data = embed_data
    
    channel_id = discord.ui.TextInput(
        label="Channel ID",
        placeholder="Enter the channel ID to send the embed to",
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel = bot.get_channel(int(self.channel_id.value))
            if not channel:
                await interaction.response.send_message("Channel not found!", ephemeral=True)
                return
            
            embed = EmbedCreatorView().create_embed()
            embed.color = self.embed_data['color']
            if self.embed_data['title']:
                embed.title = self.embed_data['title']
            if self.embed_data['description']:
                embed.description = self.embed_data['description']
            if self.embed_data['thumbnail']:
                embed.set_thumbnail(url=self.embed_data['thumbnail'])
            if self.embed_data['image']:
                embed.set_image(url=self.embed_data['image'])
            if self.embed_data['footer']:
                embed.set_footer(text=self.embed_data['footer'])
            
            message = await channel.send(embed=embed)
            embed_storage[message.id] = self.embed_data
            
            await interaction.response.send_message(f"Embed sent to {channel.mention}!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid channel ID!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error sending embed: {str(e)}", ephemeral=True)

class ReactionRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.embed_data = {
            'title': 'Role Selection',
            'description': 'React with an emoji to get a role!',
            'color': 0x0099ff
        }
        self.reaction_mappings = {}  # emoji: role_id
    
    @discord.ui.button(label="Set Embed", style=discord.ButtonStyle.blurple)
    async def set_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ReactionRoleEmbedModal(self.embed_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Add Reaction Role", style=discord.ButtonStyle.green)
    async def add_reaction(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddReactionRoleModal(self.reaction_mappings)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Create Message", style=discord.ButtonStyle.red)
    async def create_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.reaction_mappings:
            await interaction.response.send_message("Please add at least one reaction role first!", ephemeral=True)
            return
        
        modal = CreateReactionRoleMessageModal(self.embed_data, self.reaction_mappings)
        await interaction.response.send_modal(modal)

class ReactionRoleEmbedModal(discord.ui.Modal):
    def __init__(self, embed_data):
        super().__init__(title="Set Reaction Role Embed")
        self.embed_data = embed_data
        
        self.title_input = discord.ui.TextInput(
            label="Embed Title",
            default=embed_data['title'],
            max_length=256
        )
        
        self.description_input = discord.ui.TextInput(
            label="Embed Description",
            style=discord.TextStyle.paragraph,
            default=embed_data['description'],
            max_length=2000
        )
        
        self.color_input = discord.ui.TextInput(
            label="Color (hex)",
            default=hex(embed_data['color']),
            max_length=7
        )
        
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.color_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.embed_data['title'] = self.title_input.value
            self.embed_data['description'] = self.description_input.value
            color_value = self.color_input.value.replace('#', '')
            self.embed_data['color'] = int(color_value, 16)
            await interaction.response.send_message("Embed settings updated!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid color format!", ephemeral=True)

class AddReactionRoleModal(discord.ui.Modal):
    def __init__(self, reaction_mappings):
        super().__init__(title="Add Reaction Role")
        self.reaction_mappings = reaction_mappings
    
    emoji = discord.ui.TextInput(
        label="Emoji",
        placeholder="Enter emoji (e.g., 🎮, :custom_emoji:, or emoji ID)",
        max_length=50
    )
    
    role_id = discord.ui.TextInput(
        label="Role ID",
        placeholder="Enter the role ID to assign",
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            role_id = int(self.role_id.value)
            role = interaction.guild.get_role(role_id)
            if not role:
                await interaction.response.send_message("Role not found!", ephemeral=True)
                return
            
            self.reaction_mappings[self.emoji.value] = role_id
            await interaction.response.send_message(f"Added reaction role: {self.emoji.value} → {role.name}", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid role ID!", ephemeral=True)

class CreateReactionRoleMessageModal(discord.ui.Modal):
    def __init__(self, embed_data, reaction_mappings):
        super().__init__(title="Create Reaction Role Message")
        self.embed_data = embed_data
        self.reaction_mappings = reaction_mappings
    
    channel_id = discord.ui.TextInput(
        label="Channel ID",
        placeholder="Enter channel ID to post the reaction role message",
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel = bot.get_channel(int(self.channel_id.value))
            if not channel:
                await interaction.response.send_message("Channel not found!", ephemeral=True)
                return
            
            embed = discord.Embed(
                title=self.embed_data['title'],
                description=self.embed_data['description'],
                color=self.embed_data['color']
            )
            
            # Add reaction role info to embed
            roles_text = ""
            for emoji, role_id in self.reaction_mappings.items():
                role = interaction.guild.get_role(role_id)
                if role:
                    roles_text += f"{emoji} - {role.name}\n"
            
            if roles_text:
                embed.add_field(name="Available Roles", value=roles_text, inline=False)
            
            message = await channel.send(embed=embed)
            
            # Add reactions
            for emoji in self.reaction_mappings.keys():
                try:
                    await message.add_reaction(emoji)
                except:
                    pass  # Skip invalid emojis
            
            # Store reaction role mapping
            reaction_roles[message.id] = self.reaction_mappings
            
            await interaction.response.send_message(f"Reaction role message created in {channel.mention}!", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("Invalid channel ID!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error creating message: {str(e)}", ephemeral=True)

class BoostSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Set Boost Roles", style=discord.ButtonStyle.blurple)
    async def set_boost_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = BoostRolesModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="View Settings", style=discord.ButtonStyle.gray)
    async def view_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Boost Settings", color=0xff69b4)
        
        if boost_settings['roles']:
            roles_text = ""
            for boost_count, role_id in boost_settings['roles'].items():
                role = interaction.guild.get_role(role_id)
                role_name = role.name if role else "Unknown Role"
                roles_text += f"**{boost_count} boosts:** {role_name}\n"
            embed.add_field(name="Boost Roles", value=roles_text, inline=False)
        else:
            embed.add_field(name="Boost Roles", value="None configured", inline=False)
        
        tracked_users = len(boost_settings['tracking'])
        embed.add_field(name="Tracked Users", value=str(tracked_users), inline=True)
        
        total_boosts = interaction.guild.premium_subscription_count or 0
        embed.add_field(name="Current Server Boosts", value=str(total_boosts), inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class BoostRolesModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Boost Roles")
    
    role1 = discord.ui.TextInput(label="1 Boost Role ID", required=False, max_length=20)
    role2 = discord.ui.TextInput(label="2 Boosts Role ID", required=False, max_length=20)
    role3 = discord.ui.TextInput(label="3 Boosts Role ID", required=False, max_length=20)
    role4 = discord.ui.TextInput(label="4 Boosts Role ID", required=False, max_length=20)
    role5 = discord.ui.TextInput(label="5+ Boosts Role ID", required=False, max_length=20)
    
    async def on_submit(self, interaction: discord.Interaction):
        roles_data = [
            (1, self.role1.value),
            (2, self.role2.value),
            (3, self.role3.value),
            (4, self.role4.value),
            (5, self.role5.value)
        ]
        
        for boost_count, role_id_str in roles_data:
            if role_id_str:
                try:
                    role_id = int(role_id_str)
                    role = interaction.guild.get_role(role_id)
                    if role:
                        boost_settings['roles'][boost_count] = role_id
                except ValueError:
                    continue
        
        await interaction.response.send_message("Boost roles updated!", ephemeral=True)

class InviteSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Set Invite Roles", style=discord.ButtonStyle.blurple)
    async def set_invite_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = InviteRolesModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="View Settings", style=discord.ButtonStyle.gray)
    async def view_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Invite Settings", color=0x00ff00)
        
        if invite_settings['roles']:
            roles_text = ""
            for invite_count, role_id in invite_settings['roles'].items():
                role = interaction.guild.get_role(role_id)
                role_name = role.name if role else "Unknown Role"
                roles_text += f"**{invite_count} invites:** {role_name}\n"
            embed.add_field(name="Invite Roles", value=roles_text, inline=False)
        else:
            embed.add_field(name="Invite Roles", value="None configured", inline=False)
        
        tracked_users = len(invite_settings['tracking'])
        embed.add_field(name="Tracked Users", value=str(tracked_users), inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class InviteRolesModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Invite Roles")
    
    role1 = discord.ui.TextInput(label="5 Invites Role ID", required=False, max_length=20)
    role2 = discord.ui.TextInput(label="10 Invites Role ID", required=False, max_length=20)
    role3 = discord.ui.TextInput(label="25 Invites Role ID", required=False, max_length=20)
    role4 = discord.ui.TextInput(label="50 Invites Role ID", required=False, max_length=20)
    role5 = discord.ui.TextInput(label="100+ Invites Role ID", required=False, max_length=20)
    
    async def on_submit(self, interaction: discord.Interaction):
        roles_data = [
            (5, self.role1.value),
            (10, self.role2.value),
            (25, self.role3.value),
            (50, self.role4.value),
            (100, self.role5.value)
        ]
        
        for invite_count, role_id_str in roles_data:
            if role_id_str:
                try:
                    role_id = int(role_id_str)
                    role = interaction.guild.get_role(role_id)
                    if role:
                        invite_settings['roles'][invite_count] = role_id
                except ValueError:
                    continue
        
        await interaction.response.send_message("Invite roles updated!", ephemeral=True)

# Update existing classes with permission checks
class AutoresponderManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="View All", style=discord.ButtonStyle.blurple, emoji="👁️")
    async def view_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not autoresponders:
            await interaction.response.send_message("No autoresponders configured.", ephemeral=True)
            return
        
        embed = discord.Embed(title="All Autoresponders", color=0x00ff00)
        for trigger, data in autoresponders.items():
            value = f"**Response:** {data['response'][:100]}{'...' if len(data['response']) > 100 else ''}\n"
            value += f"**Type:** {'Embed' if data['is_embed'] else 'Normal'}\n"
            value += f"**Cooldown:** {data['cooldown']}s\n"
            value += f"**Roles:** {', '.join(data['allowed_roles']) if data['allowed_roles'] else 'All'}"
            embed.add_field(name=f"🔸 {trigger}", value=value, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="Edit", style=discord.ButtonStyle.gray, emoji="✏️")
    async def edit_autoresponder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not autoresponders:
            await interaction.response.send_message("No autoresponders to edit.", ephemeral=True)
            return
        
        select = EditAutoresponderSelect()
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Select an autoresponder to edit:", view=view, ephemeral=True)
    
    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, emoji="🗑️")
    async def delete_autoresponder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not autoresponders:
            await interaction.response.send_message("No autoresponders to delete.", ephemeral=True)
            return
        
        select = DeleteAutoresponderSelect()
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Select an autoresponder to delete:", view=view, ephemeral=True)

class EditAutoresponderSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=trigger, description=f"Edit: {data['response'][:50]}{'...' if len(data['response']) > 50 else ''}")
            for trigger, data in autoresponders.items()
        ]
        super().__init__(placeholder="Choose an autoresponder to edit...", options=options[:25])
    
    async def callback(self, interaction: discord.Interaction):
        trigger = self.values[0]
        if trigger in autoresponders:
            modal = EditAutoresponderModal(trigger, autoresponders[trigger])
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message("Autoresponder not found.", ephemeral=True)

class EditAutoresponderModal(discord.ui.Modal):
    def __init__(self, trigger, data):
        super().__init__(title=f"Edit Autoresponder: {trigger}")
        self.original_trigger = trigger
        
        self.trigger = discord.ui.TextInput(
            label="Trigger Word/Phrase",
            default=trigger,
            max_length=100
        )
        
        self.response = discord.ui.TextInput(
            label="Response Message",
            default=data['response'],
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        
        self.cooldown = discord.ui.TextInput(
            label="Cooldown (seconds)",
            default=str(data['cooldown']),
            max_length=10
        )
        
        self.allowed_roles = discord.ui.TextInput(
            label="Allowed Roles (comma-separated)",
            default=', '.join(data['allowed_roles']) if data['allowed_roles'] else '',
            required=False,
            max_length=500
        )
        
        self.embed_title = discord.ui.TextInput(
            label="Embed Title (optional)",
            default=data.get('embed_title', ''),
            required=False,
            max_length=256
        )
        
        self.add_item(self.trigger)
        self.add_item(self.response)
        self.add_item(self.cooldown)
        self.add_item(self.allowed_roles)
        self.add_item(self.embed_title)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            cooldown_time = int(self.cooldown.value)
        except ValueError:
            await interaction.response.send_message("Invalid cooldown value. Please enter a number.", ephemeral=True)
            return
        
        roles_list = []
        if self.allowed_roles.value:
            roles_list = [role.strip() for role in self.allowed_roles.value.split(',')]
        
        is_embed = bool(self.embed_title.value)
        
        # Remove old trigger if it changed
        if self.original_trigger != self.trigger.value.lower():
            if self.original_trigger in autoresponders:
                del autoresponders[self.original_trigger]
        
        autoresponders[self.trigger.value.lower()] = {
            'response': self.response.value,
            'cooldown': cooldown_time,
            'allowed_roles': roles_list,
            'is_embed': is_embed,
            'embed_title': self.embed_title.value,
            'last_used': 0
        }
        
        embed = discord.Embed(
            title="Autoresponder Updated",
            description=f"Successfully updated autoresponder: `{self.trigger.value}`",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class AutoresponderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Create Autoresponder", style=discord.ButtonStyle.green, emoji="➕")
    async def create_autoresponder(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CreateAutoresponderModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="List Autoresponders", style=discord.ButtonStyle.blurple, emoji="📋")
    async def list_autoresponders(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not autoresponders:
            await interaction.response.send_message("No autoresponders configured.", ephemeral=True)
            return
        
        embed = discord.Embed(title="Autoresponders", color=0x00ff00)
        for trigger, data in autoresponders.items():
            value = f"Response: {data['response'][:50]}{'...' if len(data['response']) > 50 else ''}\n"
            value += f"Type: {'Embed' if data['is_embed'] else 'Normal'}\n"
            value += f"Cooldown: {data['cooldown']}s\n"
            value += f"Roles: {', '.join(data['allowed_roles']) if data['allowed_roles'] else 'All'}"
            embed.add_field(name=trigger, value=value, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="Delete Autoresponder", style=discord.ButtonStyle.red, emoji="🗑️")
    async def delete_autoresponder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not autoresponders:
            await interaction.response.send_message("No autoresponders to delete.", ephemeral=True)
            return
        
        select = DeleteAutoresponderSelect()
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Select an autoresponder to delete:", view=view, ephemeral=True)

class CreateAutoresponderModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Create Autoresponder")
    
    trigger = discord.ui.TextInput(
        label="Trigger Word/Phrase",
        placeholder="Enter the trigger that will activate this autoresponder...",
        max_length=100
    )
    
    response = discord.ui.TextInput(
        label="Response Message",
        placeholder="Enter the response message or URL...",
        style=discord.TextStyle.paragraph,
        max_length=2000
    )
    
    cooldown = discord.ui.TextInput(
        label="Cooldown (seconds)",
        placeholder="Enter cooldown time in seconds (0 for no cooldown)",
        default="0",
        max_length=10
    )
    
    allowed_roles = discord.ui.TextInput(
        label="Allowed Roles (comma-separated)",
        placeholder="Leave empty for all roles, or enter role names separated by commas",
        required=False,
        max_length=500
    )
    
    embed_title = discord.ui.TextInput(
        label="Embed Title (optional)",
        placeholder="If you want an embed, enter the title here",
        required=False,
        max_length=256
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            cooldown_time = int(self.cooldown.value)
        except ValueError:
            await interaction.response.send_message("Invalid cooldown value. Please enter a number.", ephemeral=True)
            return
        
        roles_list = []
        if self.allowed_roles.value:
            roles_list = [role.strip() for role in self.allowed_roles.value.split(',')]
        
        is_embed = bool(self.embed_title.value)
        
        autoresponders[self.trigger.value.lower()] = {
            'response': self.response.value,
            'cooldown': cooldown_time,
            'allowed_roles': roles_list,
            'is_embed': is_embed,
            'embed_title': self.embed_title.value,
            'last_used': 0
        }
        
        embed = discord.Embed(
            title="Autoresponder Created",
            description=f"Successfully created autoresponder for trigger: `{self.trigger.value}`",
            color=0x00ff00
        )
        embed.add_field(name="Response", value=self.response.value[:100] + ('...' if len(self.response.value) > 100 else ''), inline=False)
        embed.add_field(name="Cooldown", value=f"{cooldown_time} seconds", inline=True)
        embed.add_field(name="Type", value="Embed" if is_embed else "Normal", inline=True)
        embed.add_field(name="Allowed Roles", value=', '.join(roles_list) if roles_list else "All", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class DeleteAutoresponderSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=trigger, description=f"Response: {data['response'][:50]}{'...' if len(data['response']) > 50 else ''}")
            for trigger, data in autoresponders.items()
        ]
        super().__init__(placeholder="Choose an autoresponder to delete...", options=options[:25])
    
    async def callback(self, interaction: discord.Interaction):
        trigger = self.values[0]
        if trigger in autoresponders:
            del autoresponders[trigger]
            await interaction.response.send_message(f"Deleted autoresponder for trigger: `{trigger}`", ephemeral=True)
        else:
            await interaction.response.send_message("Autoresponder not found.", ephemeral=True)

class AuctionSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Set Channel", style=discord.ButtonStyle.green, emoji="📺")
    async def set_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AuctionChannelModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Format", style=discord.ButtonStyle.blurple, emoji="📋")
    async def set_format(self, interaction: discord.Interaction, button: discord.ui.Button):
        select = FormatSelect()
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Choose auction format:", view=view, ephemeral=True)
    
    @discord.ui.button(label="View Settings", style=discord.ButtonStyle.gray, emoji="⚙️")
    async def view_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Auction Settings", color=0x0099ff)
        embed.add_field(name="Channel", value=f"<#{auction_settings['channel_id']}>" if auction_settings['channel_id'] else "Not set", inline=False)
        embed.add_field(name="Format", value=auction_settings['format'].title(), inline=False)
        if auction_settings['forum_channel_id']:
            embed.add_field(name="Forum Channel", value=f"<#{auction_settings['forum_channel_id']}>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class AuctionChannelModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Auction Channel")
    
    channel_id = discord.ui.TextInput(
        label="Channel ID",
        placeholder="Enter the channel ID where auctions will be posted",
        max_length=20
    )
    
    forum_channel_id = discord.ui.TextInput(
        label="Forum Channel ID (if using forum format)",
        placeholder="Enter forum channel ID (optional)",
        required=False,
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.channel_id.value)
            channel = bot.get_channel(channel_id)
            if not channel:
                await interaction.response.send_message("Invalid channel ID or bot doesn't have access to that channel.", ephemeral=True)
                return
            
            auction_settings['channel_id'] = channel_id
            
            if self.forum_channel_id.value:
                forum_id = int(self.forum_channel_id.value)
                forum_channel = bot.get_channel(forum_id)
                if forum_channel:
                    auction_settings['forum_channel_id'] = forum_id
            
            embed = discord.Embed(
                title="Auction Channel Set",
                description=f"Auction channel set to: {channel.mention}",
                color=0x00ff00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("Invalid channel ID format.", ephemeral=True)

class FormatSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Thread", description="Create threads for auctions", emoji="🧵"),
            discord.SelectOption(label="Channel", description="Post directly to channel", emoji="📺"),
            discord.SelectOption(label="Forum", description="Create forum posts", emoji="💬")
        ]
        super().__init__(placeholder="Choose auction format...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        format_value = self.values[0].lower()
        auction_settings['format'] = format_value
        await interaction.response.send_message(f"Auction format set to: **{format_value.title()}**", ephemeral=True)

class AuctionCreateModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Create Auction")
    
    title = discord.ui.TextInput(
        label="Auction Title",
        placeholder="Enter the auction title/thread name",
        max_length=100
    )
    
    seller_mention = discord.ui.TextInput(
        label="Seller",
        placeholder="@username or user ID",
        max_length=50
    )
    
    starting_bid = discord.ui.TextInput(
        label="Starting Bid ($)",
        placeholder="Enter starting bid amount (whole numbers only)",
        max_length=10
    )
    
    bid_increase = discord.ui.TextInput(
        label="Bid Increase ($)",
        placeholder="Minimum bid increase amount",
        max_length=10
    )
    
    instant_accept = discord.ui.TextInput(
        label="Instant Accept (IA) ($)",
        placeholder="Enter IA amount or 'NA' if not applicable",
        max_length=10
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # Store the basic info and show the extended options
        self.auction_data = {
            'title': self.title.value,
            'seller': self.seller_mention.value,
            'starting_bid': self.starting_bid.value,
            'bid_increase': self.bid_increase.value,
            'instant_accept': self.instant_accept.value
        }
        
        view = AuctionOptionsView(self.auction_data)
        embed = discord.Embed(
            title="Auction Options",
            description="Please select the auction details:",
            color=0x0099ff
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class AuctionOptionsView(discord.ui.View):
    def __init__(self, auction_data):
        super().__init__(timeout=300)
        self.auction_data = auction_data
        self.auction_data.update({
            'exo_status': 'NA',
            'sg_status': 'NA',
            'spawn_status': 'NA',
            'hold_willing': 'Ask',
            'hold_duration': '',
            'payment_methods': '',
            'duration': '',
            'images': []
        })
    
    @discord.ui.select(placeholder="EXO Status", options=[
        discord.SelectOption(label="EXO", value="exo"),
        discord.SelectOption(label="OG", value="og"),
        discord.SelectOption(label="NA", value="NA")
    ])
    async def exo_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.auction_data['exo_status'] = select.values[0]
        await interaction.response.send_message(f"EXO Status set to: {select.values[0]}", ephemeral=True)
    
    @discord.ui.select(placeholder="SG Status", options=[
        discord.SelectOption(label="SG", value="sg"),
        discord.SelectOption(label="Not SG", value="not_sg"),
        discord.SelectOption(label="NA", value="NA")
    ])
    async def sg_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.auction_data['sg_status'] = select.values[0]
        await interaction.response.send_message(f"SG Status set to: {select.values[0]}", ephemeral=True)
    
    @discord.ui.select(placeholder="Spawn Status", options=[
        discord.SelectOption(label="Spawned", value="spawned"),
        discord.SelectOption(label="Non-Spawned", value="non_spawned"),
        discord.SelectOption(label="NA", value="NA")
    ])
    async def spawn_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.auction_data['spawn_status'] = select.values[0]
        await interaction.response.send_message(f"Spawn Status set to: {select.values[0]}", ephemeral=True)
    
    @discord.ui.select(placeholder="Willing to Hold", options=[
        discord.SelectOption(label="Yes", value="yes"),
        discord.SelectOption(label="No", value="no"),
        discord.SelectOption(label="Ask", value="ask")
    ])
    async def hold_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.auction_data['hold_willing'] = select.values[0]
        await interaction.response.send_message(f"Hold willingness set to: {select.values[0]}", ephemeral=True)
    
    @discord.ui.button(label="Set Hold Duration", style=discord.ButtonStyle.gray)
    async def set_hold_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = HoldDurationModal(self.auction_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Payment Methods", style=discord.ButtonStyle.gray)
    async def set_payment_methods(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = PaymentMethodsModal(self.auction_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Duration", style=discord.ButtonStyle.gray)
    async def set_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AuctionDurationModal(self.auction_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Add Images", style=discord.ButtonStyle.gray)
    async def add_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AuctionImagesModal(self.auction_data)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Create Auction", style=discord.ButtonStyle.green)
    async def create_auction(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.post_auction(interaction)
    
    async def post_auction(self, interaction: discord.Interaction):
        if not auction_settings['channel_id']:
            await interaction.response.send_message("No auction channel set. Use /auctionsetup first.", ephemeral=True)
            return
        
        try:
            # Calculate end time (8PM EDT / 7PM EST)
            duration_parts = self.auction_data['duration'].split()
            if len(duration_parts) != 2:
                await interaction.response.send_message("Invalid duration format. Use format like '2 days' or '3 hours'", ephemeral=True)
                return
            
            amount = int(duration_parts[0])
            unit = duration_parts[1].lower()
            
            est = pytz.timezone('US/Eastern')
            now = datetime.now(est)
            
            if unit.startswith('hour'):
                end_time = now + timedelta(hours=amount)
            elif unit.startswith('day'):
                end_time = now + timedelta(days=amount)
            elif unit.startswith('week'):
                end_time = now + timedelta(weeks=amount)
            else:
                await interaction.response.send_message("Invalid time unit. Use hours, days, or weeks.", ephemeral=True)
                return
            
            # Adjust to 8PM EDT / 7PM EST
            end_time = end_time.replace(hour=20, minute=0, second=0, microsecond=0)
            
            # Create auction embed
            embed = discord.Embed(
                title=f"🔨 {self.auction_data['title']}",
                color=0xffaa00
            )
            
            embed.add_field(name="Seller", value=self.auction_data['seller'], inline=True)
            embed.add_field(name="Starting Bid", value=f"${self.auction_data['starting_bid']}", inline=True)
            embed.add_field(name="Bid Increase", value=f"${self.auction_data['bid_increase']}", inline=True)
            
            if self.auction_data['instant_accept'] != 'NA':
                embed.add_field(name="Instant Accept", value=f"${self.auction_data['instant_accept']}", inline=True)
            
            # Add status fields
            status_text = ""
            if self.auction_data['exo_status'] != 'NA':
                status_text += f"**EXO/OG:** {self.auction_data['exo_status'].upper()}\n"
            if self.auction_data['sg_status'] != 'NA':
                status_text += f"**SG Status:** {self.auction_data['sg_status'].replace('_', ' ').title()}\n"
            if self.auction_data['spawn_status'] != 'NA':
                status_text += f"**Spawn Status:** {self.auction_data['spawn_status'].replace('_', ' ').title()}\n"
            
            if status_text:
                embed.add_field(name="Item Details", value=status_text, inline=False)
            
            if self.auction_data['hold_willing'] != 'ask' or self.auction_data['hold_duration']:
                hold_text = f"**Willing to Hold:** {self.auction_data['hold_willing'].title()}"
                if self.auction_data['hold_duration']:
                    hold_text += f"\n**Hold Duration:** {self.auction_data['hold_duration']}"
                embed.add_field(name="Hold Information", value=hold_text, inline=False)
            
            if self.auction_data['payment_methods']:
                embed.add_field(name="Payment Methods", value=self.auction_data['payment_methods'], inline=False)
            
            # Add countdown
            end_timestamp = int(end_time.timestamp())
            embed.add_field(name="ENDS", value=f"<t:{end_timestamp}:R> (<t:{end_timestamp}:F>)", inline=False)
            
            embed.set_footer(text="Place your bids below!")
            
            # Get the channel and post
            channel = bot.get_channel(auction_settings['channel_id'])
            
            if auction_settings['format'] == 'thread':
                message = await channel.send(embed=embed)
                thread = await message.create_thread(name=self.auction_data['title'])
                
                # Download and post images in thread if provided
                if self.auction_data['images']:
                    for i, image_url in enumerate(self.auction_data['images'][:10]):  # Limit to 10
                        image_data = await download_image(image_url)
                        if image_data:
                            file = discord.File(io.BytesIO(image_data), filename=f"auction_image_{i+1}.png")
                            await thread.send(file=file)
                
                await interaction.response.send_message(f"Auction created successfully! Check {thread.mention}", ephemeral=True)
                
            elif auction_settings['format'] == 'forum' and auction_settings['forum_channel_id']:
                forum_channel = bot.get_channel(auction_settings['forum_channel_id'])
                if forum_channel and hasattr(forum_channel, 'create_thread'):
                    thread = await forum_channel.create_thread(
                        name=self.auction_data['title'],
                        content=None,
                        embed=embed
                    )
                    
                    if self.auction_data['images']:
                        for i, image_url in enumerate(self.auction_data['images'][:10]):
                            image_data = await download_image(image_url)
                            if image_data:
                                file = discord.File(io.BytesIO(image_data), filename=f"auction_image_{i+1}.png")
                                await thread.thread.send(file=file)
                    
                    await interaction.response.send_message(f"Auction forum post created successfully! Check {thread.thread.mention}", ephemeral=True)
                else:
                    await interaction.response.send_message("Forum channel not found or invalid.", ephemeral=True)
            else:
                message = await channel.send(embed=embed)
                
                if self.auction_data['images']:
                    for i, image_url in enumerate(self.auction_data['images'][:10]):
                        image_data = await download_image(image_url)
                        if image_data:
                            file = discord.File(io.BytesIO(image_data), filename=f"auction_image_{i+1}.png")
                            await channel.send(file=file)
                
                await interaction.response.send_message(f"Auction posted successfully in {channel.mention}", ephemeral=True)
                
        except Exception as e:
            await interaction.response.send_message(f"Error creating auction: {str(e)}", ephemeral=True)

class HoldDurationModal(discord.ui.Modal):
    def __init__(self, auction_data):
        super().__init__(title="Set Hold Duration")
        self.auction_data = auction_data
    
    duration = discord.ui.TextInput(
        label="Hold Duration",
        placeholder="e.g., '2 weeks', '1 month', '5 days'",
        max_length=50
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.auction_data['hold_duration'] = self.duration.value
        await interaction.response.send_message(f"Hold duration set to: {self.duration.value}", ephemeral=True)

class PaymentMethodsModal(discord.ui.Modal):
    def __init__(self, auction_data):
        super().__init__(title="Set Payment Methods")
        self.auction_data = auction_data
    
    methods = discord.ui.TextInput(
        label="Payment Methods",
        placeholder="e.g., PayPal, Cashapp, Zelle, etc.",
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.auction_data['payment_methods'] = self.methods.value
        await interaction.response.send_message("Payment methods set successfully!", ephemeral=True)

class AuctionDurationModal(discord.ui.Modal):
    def __init__(self, auction_data):
        super().__init__(title="Set Auction Duration")
        self.auction_data = auction_data
    
    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="e.g., '2 days', '1 week', '24 hours'",
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.auction_data['duration'] = self.duration.value
        await interaction.response.send_message(f"Auction duration set to: {self.duration.value}", ephemeral=True)

class AuctionImagesModal(discord.ui.Modal):
    def __init__(self, auction_data):
        super().__init__(title="Add Auction Images")
        self.auction_data = auction_data
    
    images = discord.ui.TextInput(
        label="Image URLs",
        placeholder="Paste up to 10 image URLs, one per line",
        style=discord.TextStyle.paragraph,
        max_length=2000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        image_urls = [url.strip() for url in self.images.value.split('\n') if url.strip()]
        self.auction_data['images'] = image_urls[:10]  # Limit to 10 images
        await interaction.response.send_message(f"Added {len(self.auction_data['images'])} image(s)", ephemeral=True)

async def update_boost_roles(member, boost_count):
    """Update roles based on boost count"""
    if not boost_settings['roles']:
        return
    
    try:
        # Remove all boost roles first
        roles_to_remove = []
        for role_id in boost_settings['roles'].values():
            role = member.guild.get_role(role_id)
            if role and role in member.roles:
                roles_to_remove.append(role)
        
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Boost role update")
        
        # Add appropriate role
        for required_boosts, role_id in sorted(boost_settings['roles'].items(), reverse=True):
            if boost_count >= required_boosts:
                role = member.guild.get_role(role_id)
                if role:
                    await member.add_roles(role, reason=f"Earned {boost_count} boosts")
                    break
    except discord.Forbidden:
        print(f"Missing permissions to manage roles for {member.display_name}")
    except discord.HTTPException as e:
        print(f"HTTP error updating boost roles for {member.display_name}: {e}")
    except Exception as e:
        print(f"Error updating boost roles for {member.display_name}: {e}")

async def update_invite_roles(member, invite_count):
    """Update roles based on invite count"""
    if not invite_settings['roles']:
        return
    
    try:
        # Remove all invite roles first
        roles_to_remove = []
        for role_id in invite_settings['roles'].values():
            role = member.guild.get_role(role_id)
            if role and role in member.roles:
                roles_to_remove.append(role)
        
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Invite role update")
        
        # Add appropriate role
        for required_invites, role_id in sorted(invite_settings['roles'].items(), reverse=True):
            if invite_count >= required_invites:
                role = member.guild.get_role(role_id)
                if role:
                    await member.add_roles(role, reason=f"Earned {invite_count} invites")
                    break
    except discord.Forbidden:
        print(f"Missing permissions to manage roles for {member.display_name}")
    except discord.HTTPException as e:
        print(f"HTTP error updating invite roles for {member.display_name}: {e}")
    except Exception as e:
        print(f"Error updating invite roles for {member.display_name}: {e}")

async def track_guild_boosts(guild):
    """Track total boosts for a guild and update all members accordingly"""
    try:
        current_boosters = [member for member in guild.members if member.premium_since]
        
        # Initialize tracking for new boosters
        for member in current_boosters:
            try:
                user_id = member.id
                if user_id not in boost_settings['tracking']:
                    boost_settings['tracking'][user_id] = {
                        'boosts': 1,
                        'boost_history': [{
                            'action': 'initial_boost',
                            'timestamp': datetime.now().isoformat()
                        }],
                        'current_boost_start': member.premium_since.isoformat() if member.premium_since else None
                    }
                elif not boost_settings['tracking'][user_id].get('current_boost_start'):
                    # User is boosting but we don't have a start time
                    boost_settings['tracking'][user_id]['current_boost_start'] = member.premium_since.isoformat() if member.premium_since else None
                
                # Update roles
                boost_count = boost_settings['tracking'][user_id]['boosts']
                await update_boost_roles(member, boost_count)
            except Exception as e:
                print(f"Error tracking boosts for member {member.id}: {e}")
                continue
    except Exception as e:
        print(f"Error tracking guild boosts for {guild.name}: {e}")

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    
    # Display guild restriction info
    if ALLOWED_GUILD_IDS:
        guild_names = []
        for guild_id in ALLOWED_GUILD_IDS:
            guild = bot.get_guild(guild_id)
            guild_names.append(guild.name if guild else f"Guild ID: {guild_id}")
        print(f"Bot restricted to guilds: {', '.join(guild_names)}")
    else:
        print("Bot will work in all servers (no guild restrictions)")
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
        
        # Cache invites for tracking
        for guild in bot.guilds:
            try:
                invites = await guild.invites()
                invite_settings['invite_cache'][guild.id] = {invite.code: invite.uses for invite in invites}
                
                # Initialize boost tracking for the guild
                await track_guild_boosts(guild)
                
            except:
                pass
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    content = message.content.lower()
    
    for trigger, data in autoresponders.items():
        if trigger in content:
            # Check cooldown
            current_time = time.time()
            if current_time - data['last_used'] < data['cooldown']:
                continue
            
            # Check roles
            if data['allowed_roles']:
                user_roles = [role.name for role in message.author.roles]
                if not any(role in user_roles for role in data['allowed_roles']):
                    continue
            
            # Send response
            if data['is_embed'] and data['embed_title']:
                embed = discord.Embed(
                    title=data['embed_title'],
                    description=data['response'],
                    color=0x0099ff
                )
                await message.channel.send(embed=embed)
            else:
                await message.channel.send(data['response'])
            
            # Update last used time
            autoresponders[trigger]['last_used'] = current_time
            break

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    
    if payload.message_id in reaction_roles:
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        emoji = str(payload.emoji)
        
        if emoji in reaction_roles[payload.message_id]:
            role_id = reaction_roles[payload.message_id][emoji]
            role = guild.get_role(role_id)
            
            if role and member:
                if role in member.roles:
                    await member.remove_roles(role)
                else:
                    await member.add_roles(role)

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return
    
    if payload.message_id in reaction_roles:
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        emoji = str(payload.emoji)
        
        if emoji in reaction_roles[payload.message_id]:
            role_id = reaction_roles[payload.message_id][emoji]
            role = guild.get_role(role_id)
            
            if role and member and role in member.roles:
                await member.remove_roles(role)

@bot.event
async def on_member_update(before, after):
    # Track boosts
    if before.premium_since != after.premium_since:
        user_id = after.id
        
        if user_id not in boost_settings['tracking']:
            boost_settings['tracking'][user_id] = {'boosts': 0, 'boost_history': [], 'current_boost_start': None}
        
        if after.premium_since and not before.premium_since:
            # User started boosting
            boost_settings['tracking'][user_id]['boosts'] += 1
            boost_settings['tracking'][user_id]['current_boost_start'] = after.premium_since.isoformat()
            boost_settings['tracking'][user_id]['boost_history'].append({
                'action': 'boost_start',
                'timestamp': datetime.now().isoformat(),
                'boost_start': after.premium_since.isoformat()
            })
        elif before.premium_since and not after.premium_since:
            # User stopped boosting - but keep their boost count for rewards
            boost_settings['tracking'][user_id]['current_boost_start'] = None
            boost_settings['tracking'][user_id]['boost_history'].append({
                'action': 'boost_end',
                'timestamp': datetime.now().isoformat(),
                'boost_end': before.premium_since.isoformat() if before.premium_since else None
            })
            # Note: We don't decrement the boost count to maintain lifetime boost tracking
        
        # Update roles based on total accumulated boosts
        boost_count = boost_settings['tracking'][user_id]['boosts']
        await update_boost_roles(after, boost_count)

@bot.event
async def on_member_join(member):
    # Check which invite was used
    try:
        invites_before = invite_settings['invite_cache'].get(member.guild.id, {})
        invites_after = await member.guild.invites()
        
        for invite in invites_after:
            if invite.code in invites_before:
                if invite.uses > invites_before[invite.code]:
                    # This invite was used
                    if invite.inviter:  # Check if inviter exists
                        inviter_id = invite.inviter.id
                        
                        if inviter_id not in invite_settings['tracking']:
                            invite_settings['tracking'][inviter_id] = {'invites': 0, 'invited_users': []}
                        
                        invite_settings['tracking'][inviter_id]['invites'] += 1
                        invite_settings['tracking'][inviter_id]['invited_users'].append({
                            'user_id': member.id,
                            'username': str(member),
                            'joined_at': datetime.now().isoformat()
                        })
                        
                        # Update invite cache
                        if member.guild.id not in invite_settings['invite_cache']:
                            invite_settings['invite_cache'][member.guild.id] = {}
                        invite_settings['invite_cache'][member.guild.id][invite.code] = invite.uses
                        
                        # Update roles for inviter
                        inviter = member.guild.get_member(inviter_id)
                        if inviter:
                            invite_count = invite_settings['tracking'][inviter_id]['invites']
                            await update_invite_roles(inviter, invite_count)
                    
                    break
            else:
                # New invite
                if member.guild.id not in invite_settings['invite_cache']:
                    invite_settings['invite_cache'][member.guild.id] = {}
                invite_settings['invite_cache'][member.guild.id][invite.code] = invite.uses
        
        # Update cache with current invites
        invite_settings['invite_cache'][member.guild.id] = {invite.code: invite.uses for invite in invites_after}
        
    except discord.Forbidden:
        print(f"Missing permissions to track invites in {member.guild.name}")
    except Exception as e:
        print(f"Error tracking member join for {member.display_name}: {e}")

@bot.event
async def on_member_remove(member):
    # Find who invited this user and decrement their count
    for inviter_id, data in invite_settings['tracking'].items():
        for invited_user in data['invited_users']:
            if invited_user['user_id'] == member.id:
                # Decrement invite count
                if invite_settings['tracking'][inviter_id]['invites'] > 0:
                    invite_settings['tracking'][inviter_id]['invites'] -= 1
                
                # Update roles for inviter
                inviter = member.guild.get_member(inviter_id)
                if inviter:
                    invite_count = invite_settings['tracking'][inviter_id]['invites']
                    await update_invite_roles(inviter, invite_count)
                
                # Mark user as left
                invited_user['left_at'] = datetime.now().isoformat()
                break

# Command definitions with permission checks
@bot.tree.command(name="config", description="Bot configuration panel")
@guild_only()
async def config_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "config", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🔧 Bot Configuration Panel",
        description="Configure bot settings and permissions:",
        color=0x7289da
    )
    embed.add_field(name="🔐 Command Permissions", value="Set role restrictions for commands", inline=False)
    embed.add_field(name="👑 Admin Roles", value="Configure administrative roles", inline=False)
    embed.add_field(name="🏠 Guild Settings", value="Configure guild restrictions", inline=False)
    embed.add_field(name="📋 View Config", value="See current configuration", inline=False)
    
    view = ConfigView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="embedcreator", description="Create custom embeds with advanced options")
@guild_only()
async def embed_creator_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "embedcreator", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🎨 Embed Creator",
        description="Create beautiful custom embeds:",
        color=0x9932cc
    )
    embed.add_field(name="📝 Set Title", value="Add a title to your embed", inline=False)
    embed.add_field(name="📄 Set Description", value="Add description and footer text", inline=False)
    embed.add_field(name="🎨 Set Color", value="Choose embed color (hex code)", inline=False)
    embed.add_field(name="🖼️ Set Images", value="Add thumbnail and bottom image", inline=False)
    embed.add_field(name="👁️ Preview", value="See how your embed looks", inline=False)
    embed.add_field(name="📤 Send to Channel", value="Post your embed", inline=False)
    
    view = EmbedCreatorView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="reactionroles", description="Create reaction role messages")
@guild_only()
async def reaction_roles_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "reactionroles", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="⚡ Reaction Roles Setup",
        description="Create messages with reaction-based role assignment:",
        color=0xff6b6b
    )
    embed.add_field(name="📝 Set Embed", value="Configure the reaction role message", inline=False)
    embed.add_field(name="➕ Add Reaction Role", value="Link emojis to specific roles", inline=False)
    embed.add_field(name="🚀 Create Message", value="Post the reaction role message", inline=False)
    
    view = ReactionRoleView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="boostsetup", description="Configure server boost tracking and roles")
@guild_only()
async def boost_setup_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "boostsetup", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🚀 Boost Tracking Setup",
        description="Configure server boost tracking and role rewards:",
        color=0xff69b4
    )
    embed.add_field(name="🎯 Set Boost Roles", value="Configure roles for different boost levels", inline=False)
    embed.add_field(name="📊 View Settings", value="See current boost configuration", inline=False)
    
    view = BoostSetupView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="invitesetup", description="Configure invite tracking and roles")
@guild_only()
async def invite_setup_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "invitesetup", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="📨 Invite Tracking Setup",
        description="Configure invite tracking and role rewards:",
        color=0x00ff00
    )
    embed.add_field(name="🎯 Set Invite Roles", value="Configure roles for different invite levels", inline=False)
    embed.add_field(name="📊 View Settings", value="See current invite configuration", inline=False)
    
    view = InviteSetupView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="invites", description="Check invite count for yourself or another member")
@app_commands.describe(member="The member to check invite count for (optional)")
@guild_only()
async def invites_command(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if not has_permission(interaction.user.roles, "invites", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    target = member or interaction.user
    user_id = target.id
    
    if user_id in invite_settings['tracking']:
        data = invite_settings['tracking'][user_id]
        invite_count = data['invites']
        
        embed = discord.Embed(
            title="📨 Invite Statistics",
            color=0x00ff00
        )
        embed.add_field(name="Member", value=target.mention, inline=True)
        embed.add_field(name="Total Invites", value=str(invite_count), inline=True)
        embed.add_field(name="Active Invites", value=str(len([u for u in data['invited_users'] if 'left_at' not in u])), inline=True)
        
        if data['invited_users']:
            recent_invites = data['invited_users'][-5:]  # Show last 5
            invite_list = ""
            for invited in recent_invites:
                status = "Left" if 'left_at' in invited else "Active"
                invite_list += f"• {invited['username']} ({status})\n"
            
            embed.add_field(name="Recent Invites", value=invite_list, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(f"{target.display_name} has no tracked invites.", ephemeral=True)

@bot.tree.command(name="autoresponder", description="Manage autoresponders with an interactive panel")
@guild_only()
async def autoresponder_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "autoresponder", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🤖 Autoresponder Management Panel",
        description="Use the buttons below to manage your autoresponders:",
        color=0x0099ff
    )
    embed.add_field(
        name="➕ Create Autoresponder",
        value="Set up a new trigger and response",
        inline=False
    )
    embed.add_field(
        name="📋 List Autoresponders",
        value="View all configured autoresponders",
        inline=False
    )
    embed.add_field(
        name="🗑️ Delete Autoresponder",
        value="Remove an existing autoresponder",
        inline=False
    )
    
    view = AutoresponderView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="autoresponders", description="Advanced autoresponder management panel")
@guild_only()
async def autoresponders_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "autoresponders", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🛠️ Advanced Autoresponder Management",
        description="Comprehensive autoresponder management tools:",
        color=0x7289da
    )
    embed.add_field(
        name="👁️ View All",
        value="See all configured autoresponders with details",
        inline=False
    )
    embed.add_field(
        name="✏️ Edit",
        value="Modify existing autoresponder settings",
        inline=False
    )
    embed.add_field(
        name="🗑️ Delete",
        value="Remove autoresponders",
        inline=False
    )
    
    view = AutoresponderManagementView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="auctionsetup", description="Setup auction system configuration")
@guild_only()
async def auction_setup(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "auctionsetup", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🔧 Auction Setup Panel",
        description="Configure your auction system settings:",
        color=0xffaa00
    )
    embed.add_field(
        name="📺 Set Channel",
        value="Configure where auctions will be posted",
        inline=False
    )
    embed.add_field(
        name="📋 Set Format",
        value="Choose between Thread, Channel, or Forum posting",
        inline=False
    )
    embed.add_field(
        name="⚙️ View Settings",
        value="See current auction configuration",
        inline=False
    )
    
    view = AuctionSetupView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="auctioncreate", description="Create a new auction with detailed options")
@guild_only()
async def auction_create(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "auctioncreate", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    modal = AuctionCreateModal()
    await interaction.response.send_modal(modal)

@bot.tree.command(name="connect4", description="Start a Connect 4 game with landmines")
@app_commands.describe(opponent="The player you want to challenge")
@guild_only()
async def connect4_command(interaction: discord.Interaction, opponent: discord.Member):
    if not has_permission(interaction.user.roles, "connect4", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    if interaction.channel.id in active_games:
        await interaction.response.send_message("There's already an active game in this channel!", ephemeral=True)
        return
    
    if opponent == interaction.user:
        await interaction.response.send_message("You can't play against yourself!", ephemeral=True)
        return
    
    if opponent.bot:
        await interaction.response.send_message("You can't play against a bot!", ephemeral=True)
        return
    
    # Create new game
    game = Connect4Game(interaction.user, opponent, interaction.channel)
    active_games[interaction.channel.id] = game
    
    embed = discord.Embed(
        title="🎮 Connect 4 with Landmines",
        description=f"**{interaction.user.mention}** vs **{opponent.mention}**\n\nCurrent turn: {game.current_player.mention}",
        color=0x0099ff
    )
    embed.add_field(name="How to Play", value="• Connect 4 pieces in a row to win\n• Watch out for hidden landmines! 💥\n• Hitting a landmine costs you 2 turns\n• Click column numbers to drop pieces", inline=False)
    embed.add_field(name="Board", value=game.get_board_display(), inline=False)
    embed.set_footer(text="Click a column number to drop your piece!")
    
    view = Connect4View(game)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="endgame", description="End the current Connect 4 game")
@guild_only()
async def end_game_command(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "endgame", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    if interaction.channel.id not in active_games:
        await interaction.response.send_message("No active game in this channel!", ephemeral=True)
        return
    
    game = active_games[interaction.channel.id]
    
    if interaction.user not in [game.player1, game.player2]:
        await interaction.response.send_message("Only players can end the game!", ephemeral=True)
        return
    
    del active_games[interaction.channel.id]
    
    embed = discord.Embed(
        title="Game Ended",
        description=f"Game ended by {interaction.user.mention}",
        color=0xff0000
    )
    embed.add_field(name="Final Board", value=game.get_board_display(), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="test_autoresponder", description="Test an autoresponder trigger")
@app_commands.describe(trigger="The trigger word to test")
@guild_only()
async def test_autoresponder(interaction: discord.Interaction, trigger: str):
    if not has_permission(interaction.user.roles, "test_autoresponder", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    trigger_lower = trigger.lower()
    
    if trigger_lower not in autoresponders:
        await interaction.response.send_message(f"No autoresponder found for trigger: `{trigger}`", ephemeral=True)
        return
    
    data = autoresponders[trigger_lower]
    
    # Check roles
    if data['allowed_roles']:
        user_roles = [role.name for role in interaction.user.roles]
        if not any(role in user_roles for role in data['allowed_roles']):
            await interaction.response.send_message("You don't have permission to use this autoresponder.", ephemeral=True)
            return
    
    # Send test response
    if data['is_embed'] and data['embed_title']:
        embed = discord.Embed(
            title=f"🧪 TEST: {data['embed_title']}",
            description=data['response'],
            color=0xff9900
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(f"🧪 TEST RESPONSE:\n{data['response']}", ephemeral=True)

@bot.tree.command(name="export_autoresponders", description="Export autoresponders configuration")
@guild_only()
async def export_autoresponders(interaction: discord.Interaction):
    if not has_permission(interaction.user.roles, "export_autoresponders", interaction.user):
        await interaction.response.send_message("❌ You need Administrator permissions or be assigned to specific roles to use this command.", ephemeral=True)
        return
    
    if not autoresponders:
        await interaction.response.send_message("No autoresponders to export.", ephemeral=True)
        return
    
    try:
        # Remove timestamps for export
        export_data = {}
        for trigger, data in autoresponders.items():
            export_data[trigger] = {
                'response': data['response'],
                'cooldown': data['cooldown'],
                'allowed_roles': data['allowed_roles'],
                'is_embed': data['is_embed'],
                'embed_title': data['embed_title']
            }
        
        json_data = json.dumps(export_data, indent=2)
        
        # Create file in memory instead of writing to disk
        file_buffer = io.BytesIO(json_data.encode('utf-8'))
        file = discord.File(file_buffer, filename='autoresponders_export.json')
        
        await interaction.response.send_message("Here's your autoresponders configuration:", file=file, ephemeral=True)
        
    except Exception as e:
        await interaction.response.send_message(f"Error exporting autoresponders: {str(e)}", ephemeral=True)

# Load configuration from environment variables at startup
import os

# Load configuration from environment variables
token = os.getenv('DISCORD_BOT_TOKEN')
guild_ids_env = os.getenv('ALLOWED_GUILD_IDS')

# Set guild restrictions if provided
if guild_ids_env:
    try:
        ALLOWED_GUILD_IDS = [int(guild_id.strip()) for guild_id in guild_ids_env.split(',') if guild_id.strip()]
        print(f"Bot restricted to guild IDs: {ALLOWED_GUILD_IDS}")
    except ValueError:
        print("Invalid ALLOWED_GUILD_IDS format. Bot will run without guild restriction.")
        ALLOWED_GUILD_IDS = []
else:
    ALLOWED_GUILD_IDS = []

# Run the bot
if __name__ == "__main__":
    if not token:
        print("ERROR: Please set the DISCORD_BOT_TOKEN environment variable")
        print("You can do this in the Secrets tab in Replit")
        print("\nSteps to fix:")
        print("1. Click on the 'Secrets' tab in the sidebar")
        print("2. Add a new secret with key: DISCORD_BOT_TOKEN")
        print("3. Set the value to your Discord bot token")
        print("4. Run the bot again")
        print("\nOptional: Set ALLOWED_GUILD_IDS to restrict bot to specific servers")
        print("Format: comma-separated guild IDs (e.g., '123456789,987654321')")
        exit(1)
    else:
        print(f"Starting Discord bot...")
        if ALLOWED_GUILD_IDS:
            print(f"Bot restricted to guild IDs: {ALLOWED_GUILD_IDS}")
        else:
            print("Bot will work in all servers (no guild restrictions)")
        
        # Initialize data structures
        if 'invite_cache' not in invite_settings:
            invite_settings['invite_cache'] = {}
        if 'tracking' not in boost_settings:
            boost_settings['tracking'] = {}
        if 'tracking' not in invite_settings:
            invite_settings['tracking'] = {}
        
        try:
            bot.run(token)
        except discord.LoginFailure:
            print("ERROR: Invalid bot token. Please check your DISCORD_BOT_TOKEN secret.")
            exit(1)
        except Exception as e:
            print(f"Error starting bot: {e}")
            exit(1)
