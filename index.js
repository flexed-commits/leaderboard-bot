/**
 * Automated Weekly Leaderboard Bot (Discord.js)
 *
 * This script handles persistence of configuration and timer state
 * using a single 'botConfig' object, simulating a JSON database file.
 *
 * NOTE: For true persistence across restarts, you would need to use a
 * file system (Node's 'fs') or a database (like Firestore) to read/write
 * the 'botConfig' object.
 *
 * This bot requires the 'MESSAGE_CONTENT' intent for message counting
 * and 'GUILD_MEMBERS' for role management.
 *
 * Requirements:
 * 1. Node.js (v18+)
 * 2. discord.js (v14)
 * 3. A Discord bot token and Application ID.
 *
 * To run:
 * 1. npm install discord.js
 * 2. Set DISCORD_BOT_TOKEN and DISCORD_CLIENT_ID environment variables.
 * 3. node index.js
 */

import { Client, GatewayIntentBits, Partials, Routes, REST, SlashCommandBuilder } from 'discord.js';
import { setTimeout } from 'timers/promises';

// --- CONFIGURATION AND SIMULATED DATABASE ---
let botConfig = {
    setupComplete: false,
    leaderboardChannelId: null,
    topRoleToGrantId: null,
    topUserCount: 3,
    sourceChannelId: null,
    lastRunTimestamp: 0, // Unix timestamp of the last successful leaderboard run
    nextRunTimestamp: 0, // Unix timestamp of the next scheduled run
};

// Replace with your actual Bot Token and Client ID (use environment variables in production)
const DISCORD_BOT_TOKEN = process.env.DISCORD_BOT_TOKEN || 'YOUR_BOT_TOKEN_HERE';
const DISCORD_CLIENT_ID = process.env.DISCORD_CLIENT_ID || 'YOUR_CLIENT_ID_HERE';

// Utility function to simulate saving the JSON config
function setStorage(newConfig) {
    botConfig = { ...botConfig, ...newConfig };
    console.log('[STORAGE] Configuration updated:', botConfig);
}

// --- UTILITIES & SCHEDULER LOGIC ---

/**
 * Calculates the exact Unix timestamp (ms) for the next Sunday at 04:30 AM GMT.
 * This logic ensures continuity even if the bot restarts mid-week, adhering to the schedule.
 * @returns {{timestamp: number, delay: number}} The next run time and the delay in ms.
 */
function calculateNextRunTime() {
    const now = new Date();
    // Get current time in UTC
    const nowUtc = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), now.getUTCHours(), now.getUTCMinutes(), now.getUTCSeconds(), now.getUTCMilliseconds()));

    // Target Time: Sunday (0) at 04:30 AM UTC (GMT)
    const targetDay = 0; // Sunday
    const targetHour = 4;
    const targetMinute = 30;

    // Start with the current time set to the target time for today
    const nextRun = new Date(Date.UTC(
        nowUtc.getUTCFullYear(),
        nowUtc.getUTCMonth(),
        nowUtc.getUTCDate(),
        targetHour,
        targetMinute,
        0,
        0
    ));

    // Calculate the difference in days to the next Sunday
    let daysToAdd = (7 + targetDay - nowUtc.getUTCDay()) % 7;

    // Special case: If today is Sunday (daysToAdd === 0), check if 04:30 AM has passed
    if (daysToAdd === 0 && nowUtc.getUTCHours() * 60 + nowUtc.getUTCMinutes() >= targetHour * 60 + targetMinute) {
        // If it's Sunday and 4:30 AM has passed, schedule for next Sunday (add 7 days)
        daysToAdd = 7;
    }

    // Adjust the date by the required number of days
    nextRun.setUTCDate(nextRun.getUTCDate() + daysToAdd);

    const delay = nextRun.getTime() - nowUtc.getTime();

    return {
        timestamp: nextRun.getTime(),
        delay: delay
    };
}

let schedulerTimeout;

/**
 * Initializes and manages the weekly scheduler.
 * @param {Client} client The Discord client instance.
 */
function startScheduler(client) {
    if (!botConfig.setupComplete) {
        console.log('[SCHEDULER] Setup incomplete. Scheduler not started.');
        return;
    }

    const { timestamp, delay } = calculateNextRunTime();
    setStorage({ nextRunTimestamp: timestamp });

    console.log(`[SCHEDULER] Next run scheduled for: ${new Date(timestamp).toUTCString()}`);
    console.log(`[SCHEDULER] Delay: ${Math.round(delay / 1000 / 60)} minutes.`);

    if (schedulerTimeout) {
        clearTimeout(schedulerTimeout);
    }

    schedulerTimeout = setTimeout(async () => {
        try {
            console.log('[SCHEDULER] Executing scheduled leaderboard update...');
            await runLeaderboardUpdate(client);
        } catch (error) {
            console.error('[SCHEDULER ERROR] Failed to run scheduled update:', error);
        } finally {
            // Reschedule the next run immediately after the current one completes
            startScheduler(client);
        }
    }, delay);
}


// --- REUSABLE MESSAGE COUNTING LOGIC ---

/**
 * Fetches messages and calculates message counts for the last 7 days.
 * @param {GuildChannel} sourceChannel The channel to count messages from.
 * @returns {Promise<Map<string, number>>} A promise that resolves to a map of user IDs to message counts.
 */
async function getWeeklyMessageCounts(sourceChannel) {
    const messageCounts = new Map();
    const ONE_WEEK_MS = 7 * 24 * 60 * 60 * 1000;
    const cutoffTime = Date.now() - ONE_WEEK_MS;

    let lastId;
    let fetchedMessages = 0;

    // Fetch messages until we hit the cutoff time (max 5000 messages to prevent excessive API calls)
    while (fetchedMessages < 5000) {
        const messages = await sourceChannel.messages.fetch({ limit: 100, before: lastId });

        if (messages.size === 0) break;

        for (const message of messages.values()) {
            if (message.createdTimestamp < cutoffTime) {
                // Stop iterating when we reach messages older than 7 days
                fetchedMessages = 5001; // Flag to exit outer loop
                break;
            }

            // Exclude bot messages
            if (!message.author.bot) {
                const userId = message.author.id;
                const currentCount = messageCounts.get(userId) || 0;
                messageCounts.set(userId, currentCount + 1);
            }
        }

        if (fetchedMessages > 5000) break;

        lastId = messages.last().id;
        fetchedMessages += messages.size;
    }

    return messageCounts;
}


// --- CORE LEADERBOARD LOGIC ---

/**
 * Fetches messages, calculates the leaderboard, manages roles, and sends the message.
 * @param {Client} client The Discord client instance.
 * @param {boolean} isTest Flag to indicate if this is a manual test run.
 * @param {object} interaction The interaction object if it's a test run.
 */
async function runLeaderboardUpdate(client, isTest = false, interaction = null) {
    const config = botConfig;
    if (!config.setupComplete) {
        if (interaction) await interaction.reply({ content: 'The auto-leaderboard is not yet set up. Please use `/setup-auto-leaderboard` first.', ephemeral: true });
        return;
    }

    // Determine the guild (assuming single-server usage)
    const guildId = interaction ? interaction.guildId : client.guilds.cache.firstKey();
    const guild = client.guilds.cache.get(guildId);
    if (!guild) {
        console.error('Guild not found.');
        if (interaction) await interaction.reply({ content: 'Error: Guild not found.', ephemeral: true });
        return;
    }

    let reply = interaction ? await interaction.deferReply() : null;

    try {
        const leaderboardChannel = guild.channels.cache.get(config.leaderboardChannelId);
        const sourceChannel = guild.channels.cache.get(config.sourceChannelId);
        const topRole = guild.roles.cache.get(config.topRoleToGrantId);

        if (!leaderboardChannel || !sourceChannel || !topRole) {
            const errorMsg = 'Setup configuration is invalid (Channel/Role not found). Please run `/setup-auto-leaderboard` again.';
            if (interaction) await interaction.editReply({ content: errorMsg });
            else console.error(errorMsg);
            return;
        }

        // 1. Fetch and count messages using the reusable function
        const messageCounts = await getWeeklyMessageCounts(sourceChannel);

        // 2. Sort and get top users
        const sortedUsers = Array.from(messageCounts.entries())
            .sort((a, b) => b[1] - a[1]) // Sort by count descending
            .slice(0, config.topUserCount); // Take the top N

        // 3. Role Management: Clear and Grant
        const topUserIds = sortedUsers.map(u => u[0]);
        // Fetch all members to clear the role easily
        const members = await guild.members.fetch();
        const roleName = topRole.name;

        // Clear the role from ALL members who currently have it
        console.log(`[ROLES] Clearing role "${roleName}" from all members...`);
        for (const member of members.values()) {
            if (member.roles.cache.has(config.topRoleToGrantId)) {
                await member.roles.remove(topRole, 'Weekly leaderboard role clearance.');
            }
        }

        // Grant the role to the top users
        console.log(`[ROLES] Granting role "${roleName}" to top ${topUserIds.length} members...`);
        for (const userId of topUserIds) {
            const member = members.get(userId);
            if (member) {
                await member.roles.add(topRole, 'Weekly leaderboard top user award.');
            }
        }

        // 4. Generate and send the message
        let top1, top2, top3;

        // Assign top users, defaulting to placeholders if less than 3 are found
        top1 = sortedUsers[0] ? `<@${sortedUsers[0][0]}> with **${sortedUsers[0][1]}** messages` : 'N/A (No user ranked)';
        top2 = sortedUsers[1] ? `<@${sortedUsers[1][0]}> with **${sortedUsers[1][1]}** messages` : 'N/A (No user ranked)';
        top3 = sortedUsers[2] ? `<@${sortedUsers[2][0]}> with **${sortedUsers[2][1]}** messages` : 'N/A (No user ranked)';

        // Custom rewards message generation
        const leaderboardText = `Hello fellas, 
We're back with the weekly leaderboard update!! <a:Pika_Think:1444211873687011328>

Here are the top ${config.topUserCount} active members past week–
:first_place: Top 1: ${top1}. 
-# Gets 50k unb in cash
:second_place: Top 2: ${top2}.
-# Gets 25k unb in cash
:third_place: Top 3: ${top3}.
-# Gets 10k unb in cash

All of the top three members have been granted the role:
**${roleName}**

Top 1 can change their server nickname once. Top 1 & 2 can have a custom role with name and colour based on their requests. Contact <@1193415556402008169> (<@&1405157360045002785>) within 24 hours to claim your awards.`;

        await leaderboardChannel.send(leaderboardText);

        if (interaction) {
            await interaction.editReply({ content: `✅ Leaderboard successfully run and posted to <#${config.leaderboardChannelId}>. Roles have been updated.` });
        }

        // Update last run time only on successful scheduled runs (not on test runs)
        if (!isTest) {
            setStorage({ lastRunTimestamp: Date.now() });
        }

    } catch (error) {
        console.error('Error during leaderboard update:', error);
        const errorMsg = `An error occurred while running the leaderboard update: \`${error.message}\``;
        if (interaction) await interaction.editReply({ content: errorMsg });
    }
}


// --- DISCORD SETUP & COMMAND REGISTRATION ---

const commands = [
    new SlashCommandBuilder()
        .setName('setup-auto-leaderboard')
        .setDescription('Sets up the automated weekly leaderboard system.')
        .addChannelOption(option =>
            option.setName('channel')
                .setDescription('The channel where the final leaderboard message will be sent.')
                .setRequired(true))
        .addChannelOption(option =>
            option.setName('from_channel')
                .setDescription('The channel to count messages from (e.g., #general).')
                .setRequired(true))
        .addRoleOption(option =>
            option.setName('role')
                .setDescription('The role to clear and then give to the top members.')
                .setRequired(true))
        .addIntegerOption(option =>
            option.setName('top')
                .setDescription('The number of top users to fetch (e.g., 3). Must be 1 or more.')
                .setRequired(true)
                .setMinValue(1))
        .setDefaultMemberPermissions(0) // Admin only
        .toJSON(),

    new SlashCommandBuilder()
        .setName('test-leaderboard')
        .setDescription('Manually runs the leaderboard update immediately for testing.')
        .setDefaultMemberPermissions(0)
        .toJSON(),

    new SlashCommandBuilder()
        .setName('leaderboard-timer')
        .setDescription('Shows the time remaining until the next scheduled leaderboard update.')
        .toJSON(),

    new SlashCommandBuilder()
        .setName('stats')
        .setDescription('Shows message statistics (total messages and top user) for the last 7 days.')
        .toJSON(),
];

async function registerCommands(client) {
    const rest = new REST({ version: '10' }).setToken(DISCORD_BOT_TOKEN);
    try {
        // Register commands globally (or replace with guildCommands for faster development)
        await rest.put(
            Routes.applicationCommands(DISCORD_CLIENT_ID),
            { body: commands },
        );
        console.log('[COMMANDS] Successfully registered application commands.');
    } catch (error) {
        console.error('[COMMANDS ERROR] Failed to register application commands:', error);
    }
}

// --- BOT CLIENT INITIALIZATION ---

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMembers, // For managing roles
        GatewayIntentBits.GuildMessages, // For message fetching
        GatewayIntentBits.MessageContent, // REQUIRED for reading message content to count
    ],
    partials: [Partials.Channel, Partials.GuildMember]
});

client.on('ready', () => {
    console.log(`[BOT] Logged in as ${client.user.tag}!`);

    // Register commands globally
    registerCommands(client);

    // Start the scheduler based on the persistent config
    startScheduler(client);
});

client.on('interactionCreate', async interaction => {
    if (!interaction.isCommand()) return;

    const { commandName } = interaction;

    // --- 1. SETUP COMMAND ---
    if (commandName === 'setup-auto-leaderboard') {
        if (!interaction.memberPermissions.has('ADMINISTRATOR')) {
            return interaction.reply({ content: 'You must be an administrator to use this command.', ephemeral: true });
        }

        const lbChannel = interaction.options.getChannel('channel');
        const role = interaction.options.getRole('role');
        const topCount = interaction.options.getInteger('top');
        const sourceChannel = interaction.options.getChannel('from_channel');

        if (lbChannel.type !== 0 || sourceChannel.type !== 0) { // 0 is GuildText
            return interaction.reply({ content: 'Both channel and from_channel must be text channels.', ephemeral: true });
        }

        const { timestamp } = calculateNextRunTime();

        setStorage({
            setupComplete: true,
            leaderboardChannelId: lbChannel.id,
            topRoleToGrantId: role.id,
            topUserCount: topCount,
            sourceChannelId: sourceChannel.id,
            // Reset lastRunTimestamp to 0, forcing a clean start for the next run
            lastRunTimestamp: 0,
            nextRunTimestamp: timestamp
        });

        // Restart the scheduler with the new config
        startScheduler(client);

        await interaction.reply({
            content: `✅ Leaderboard setup complete!
- Leaderboard Channel: <#${lbChannel.id}>
- Messages Counted From: <#${sourceChannel.id}>
- Top Users: ${topCount}
- Role to Grant: **${role.name}**
- Next Scheduled Update: **${new Date(timestamp).toUTCString()}**`,
            ephemeral: true
        });
    }

    // --- 2. TEST COMMAND ---
    else if (commandName === 'test-leaderboard') {
        if (!interaction.memberPermissions.has('ADMINISTRATOR')) {
            return interaction.reply({ content: 'You must be an administrator to use this command.', ephemeral: true });
        }
        await runLeaderboardUpdate(client, true, interaction);
    }

    // --- 3. TIMER COMMAND ---
    else if (commandName === 'leaderboard-timer') {
        if (!botConfig.setupComplete) {
            return interaction.reply({ content: 'The auto-leaderboard is not yet set up.', ephemeral: true });
        }

        const now = Date.now();
        let nextRunTimestamp = botConfig.nextRunTimestamp;

        // Recalculate if the stored time is in the past (e.g., missed run during downtime)
        if (nextRunTimestamp < now) {
            const { timestamp } = calculateNextRunTime();
            setStorage({ nextRunTimestamp: timestamp });
            nextRunTimestamp = timestamp;
        }

        const delay = nextRunTimestamp - now;
        const totalSeconds = Math.floor(delay / 1000);
        const days = Math.floor(totalSeconds / (3600 * 24));
        const hours = Math.floor((totalSeconds % (3600 * 24)) / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;

        await interaction.reply({
            content: `⏳ The next automated leaderboard update is scheduled for **${new Date(nextRunTimestamp).toUTCString()}**.
(In **${days}** days, **${hours}** hours, **${minutes}** minutes, and **${seconds}** seconds).`,
            ephemeral: true
        });
    }
    
    // --- 4. STATS COMMAND ---
    else if (commandName === 'stats') {
        if (!botConfig.setupComplete) {
            return interaction.reply({ content: 'The auto-leaderboard is not yet set up. Please use `/setup-auto-leaderboard` first.', ephemeral: true });
        }

        await interaction.deferReply({ ephemeral: true });

        const config = botConfig;
        const guild = interaction.guild;
        const sourceChannel = guild.channels.cache.get(config.sourceChannelId);

        if (!sourceChannel) {
            return interaction.editReply({ content: 'The source channel configured for message counting was not found. Please re-run `/setup-auto-leaderboard`.' });
        }

        try {
            const messageCounts = await getWeeklyMessageCounts(sourceChannel);

            const totalMessages = Array.from(messageCounts.values()).reduce((sum, count) => sum + count, 0);

            // Get top user
            const topUserEntry = Array.from(messageCounts.entries())
                .sort((a, b) => b[1] - a[1])[0];

            let topUserText = 'No active members found in the last 7 days.';
            if (topUserEntry) {
                const [userId, count] = topUserEntry;
                topUserText = `<@${userId}> with **${count}** messages.`;
            }

            // Format the start and end date for clarity (7 days ago to now)
            const today = new Date();
            const sevenDaysAgo = new Date(today.getTime() - (7 * 24 * 60 * 60 * 1000));
            
            const formatDate = (date) => date.toLocaleDateString('en-US', {
                month: 'short',
                day: 'numeric',
                year: 'numeric'
            });

            const statsMessage = `📊 **Weekly Message Statistics**
Period: **${formatDate(sevenDaysAgo)}** to **${formatDate(today)}** (Last 7 days)

**Source Channel:** <#${sourceChannel.id}>

**Total Messages Sent:** **${totalMessages}**
**Most Active Member:** ${topUserText}`;

            await interaction.editReply({ content: statsMessage });

        } catch (error) {
            console.error('Error during /stats command:', error);
            await interaction.editReply({ content: `An error occurred while fetching stats: \`${error.message}\`` });
        }
    }
});

// Final check before login
if (DISCORD_BOT_TOKEN === 'YOUR_BOT_TOKEN_HERE' || DISCORD_CLIENT_ID === 'YOUR_CLIENT_ID_HERE') {
    console.error('!!! CRITICAL: Please set your DISCORD_BOT_TOKEN and DISCORD_CLIENT_ID.');
    process.exit(1);
}

client.login(DISCORD_BOT_TOKEN).catch(err => {
    console.error('Failed to log in to Discord:', err);
});