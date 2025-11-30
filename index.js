/**
 * DISCORD LEADERBOARD SCHEDULER BOT (Discord.js v14)
 * * This script implements a weekly message counter and role assignment bot 
 * that runs every Sunday at 10:00 AM IST.
 * * To run this:
 * 1. Install dependencies: npm install discord.js
 * 2. Set your DISCORD_TOKEN environment variable.
 */

import { Client, GatewayIntentBits, REST, Routes } from 'discord.js';

// --- CONFIGURATION ---
// IMPORTANT: Replace the placeholder token with your actual bot token.
const DISCORD_TOKEN = process.env.DISCORD_TOKEN || 'YOUR_BOT_TOKEN_HERE';
const CLIENT_ID = '1444390862095126538'; // Get this from the Discord Developer Portal

// Scheduling Constants (IST: UTC+5:30)
const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;
const TARGET_HOUR = 10;     // 10 AM IST
const TARGET_MINUTE = 0;
const CHECK_INTERVAL_MS = 30 * 60 * 1000; // Check every 30 minutes

// In-memory configuration storage (Replace with FS/Firestore for persistence)
// Key: Guild ID, Value: Config Object
const botConfig = {}; 

// --- TIME UTILITIES ---

/**
 * Calculates the next Sunday at 10:00 AM IST, returning a UTC Date object.
 * @returns {Date} The next scheduled run time in UTC.
 */
function getNextSunday10AMIST() {
    const now = new Date();
    
    // 1. Get current time adjusted to IST (Conceptually)
    const nowUtcMs = now.getTime();
    const nowIstDate = new Date(nowUtcMs + IST_OFFSET_MS);

    // 2. Calculate days until the next Sunday (0 is Sunday in JS getDay())
    let daysUntilSunday = 0 - nowIstDate.getDay();
    if (daysUntilSunday < 0) {
        daysUntilSunday += 7;
    }

    // 3. Set the date for the target Sunday
    const targetDate = new Date(nowIstDate);
    targetDate.setDate(nowIstDate.getDate() + daysUntilSunday);

    // 4. Set the exact time (10:00 AM IST)
    targetDate.setHours(TARGET_HOUR);
    targetDate.setMinutes(TARGET_MINUTE);
    targetDate.setSeconds(0);
    targetDate.setMilliseconds(0);

    // 5. Correct for today being Sunday and time already passed
    // If daysUntilSunday was 0, and the target time is in the past, move to next week
    if (daysUntilSunday === 0 && targetDate.getTime() <= nowIstDate.getTime()) {
        targetDate.setDate(targetDate.getDate() + 7);
    }
    
    // 6. Convert the calculated IST time back to a UTC Date object
    const targetUtcMs = targetDate.getTime() - IST_OFFSET_MS;
    return new Date(targetUtcMs);
}

// --- BOT CLIENT SETUP ---

const client = new Client({ 
    intents: [
        GatewayIntentBits.Guilds, 
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
        GatewayIntentBits.GuildMembers // Required for role management
    ] 
});

// --- CORE LEADERBOARD LOGIC ---

/**
 * Executes the full leaderboard process for a guild.
 * @param {import('discord.js').Guild} guild - The Discord Guild object.
 * @param {object} config - The guild's configuration.
 * @param {import('discord.js').TextChannel} fromChannel - The channel to count messages from.
 * @param {import('discord.js').TextChannel} targetChannel - The channel to post the message in.
 * @param {import('discord.js').Role} winnerRole - The role to assign.
 */
async function runLeaderboardLogic(guild, config, fromChannel, targetChannel, winnerRole) {
    console.log(`[LOGIC] Starting leaderboard run for guild: ${guild.name} (${guild.id})`);

    const sevenDaysAgo = new Date();
    sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
    
    const messageCounts = new Map();
    let totalMessages = 0;

    // 1. Message Counting
    try {
        let lastId = null;
        let running = true;
        
        while (running) {
            // Fetch messages in chunks of 100
            const messages = await fromChannel.messages.fetch({ 
                limit: 100, 
                before: lastId 
            });

            if (messages.size === 0) {
                running = false;
                break;
            }

            for (const [id, message] of messages) {
                // Stop if we hit the 7-day threshold
                if (message.createdAt.getTime() < sevenDaysAgo.getTime()) {
                    running = false;
                    break;
                }
                
                if (!message.author.bot) {
                    const userId = message.author.id;
                    const currentCount = messageCounts.get(userId) || 0;
                    messageCounts.set(userId, currentCount + 1);
                    totalMessages++;
                }
            }
            lastId = messages.last()?.id;
            
            // Safety break to prevent infinite loops, though the time check should handle it
            if (totalMessages > 100000) { 
                console.warn('[LOGIC] Message count exceeded 100,000. Breaking early.');
                running = false; 
            }
        }
    } catch (error) {
        console.error(`[ERROR] Failed to fetch messages in ${fromChannel.name}:`, error);
        return false;
    }

    // Convert Map to array and sort
    const topMembers = Array.from(messageCounts.entries())
        .sort((a, b) => b[1] - a[1])
        .slice(0, config.topN);

    // 2. Role Management: Reset
    console.log('[LOGIC] Removing role from previous winners...');
    const membersWithRole = winnerRole.members;
    for (const [memberId, member] of membersWithRole) {
        try {
            await member.roles.remove(winnerRole, 'Weekly Leaderboard Reset');
        } catch (error) {
            console.error(`[ERROR] Failed to remove role from ${member.user.tag}:`, error.message);
        }
    }

    // 3. Role Management: Assignment
    const topUsers = [];
    console.log('[LOGIC] Assigning role to new winners...');
    for (const [userId, count] of topMembers) {
        const member = await guild.members.fetch(userId).catch(() => null);
        if (member) {
            try {
                await member.roles.add(winnerRole, 'Weekly Leaderboard Winner');
                topUsers.push({ user: member.user, count });
            } catch (error) {
                console.error(`[ERROR] Failed to add role to ${member.user.tag}:`, error.message);
            }
        }
    }

    // 4. Build and Post Message
    const getWinnerData = (index) => {
        if (index < topUsers.length) {
            const { user, count } = topUsers[index];
            return { mention: user.toString(), count };
        }
        return { mention: 'N/A', count: 0 };
    };

    const w1 = getWinnerData(0);
    const w2 = getWinnerData(1);
    const w3 = getWinnerData(2);

    const messageContent = `
Hello fellas, 
We're back with the weekly leaderboard update!! <:Pika_Think:1444211873687011328>

Here are the top ${config.topN} active members past week:
:first_place: Top 1: ${w1.mention} with ${w1.count} messages. 
-# Gets 50k unb in cash
:second_place: Top 2: ${w2.mention} with ${w2.count} messages.
-# Gets 25k unb in cash
:third_place: Top 3: ${w3.mention} with ${w3.count} messages.
-# Gets 10k unb in cash

All of the top three members have been granted the role:
${winnerRole.toString()}

Top 1 can change their server nickname once. Top 1 & 2 can have a custom role with name and colour based on their requests. Contact <@1193415556402008169> (<@&1405157360045002785>) within 24 hours to claim your awards.
    `;

    try {
        await targetChannel.send(messageContent);
        console.log(`[LOGIC] Leaderboard message posted successfully.`);
    } catch (error) {
        console.error(`[ERROR] Failed to post message in ${targetChannel.name}:`, error.message);
        return false;
    }
    
    // 5. Update Next Schedule Time
    const newNextRun = getNextSunday10AMIST();
    config.nextRunDt = newNextRun.toISOString();
    
    // NOTE: In a real app, you would save botConfig back to a JSON file or Firestore here.
    // fs.writeFileSync('config.json', JSON.stringify(botConfig, null, 2));

    console.log(`[LOGIC] Next run scheduled for: ${newNextRun.toISOString()}`);
    return true;
}

// --- COMMAND DATA ---

const commands = [
    {
        name: 'setup-leaderboard',
        description: 'Configure the weekly message leaderboard system.',
        options: [
            {
                name: 'winner_channel',
                type: 7, // CHANNEL type
                description: 'The channel where the final leaderboard message will be sent.',
                required: true,
            },
            {
                name: 'winner_role',
                type: 8, // ROLE type
                description: 'The role to give to the top active members.',
                required: true,
            },
            {
                name: 'top_n',
                type: 4, // INTEGER type
                description: 'How many members to award (e.g., 3).',
                required: true,
                minValue: 1,
                maxValue: 50,
            },
            {
                name: 'count_channel',
                type: 7, // CHANNEL type
                description: 'The channel to count messages from (e.g., #general).',
                required: true,
            },
        ],
    },
    {
        name: 'test-leaderboard',
        description: 'Immediately run the configured leaderboard logic (Admin only).',
    },
    {
        name: 'leaderboard-timer',
        description: 'Shows the remaining time until the next automatic leaderboard run.',
    },
];

// --- BOT EVENTS ---

client.on('ready', async () => {
    console.log(`Logged in as ${client.user.tag}!`);

    // Register slash commands globally or per guild
    const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
    try {
        console.log('Started refreshing application (/) commands.');
        // Use client.application.id if registering globally
        await rest.put(
            Routes.applicationCommands(CLIENT_ID),
            { body: commands },
        );
        console.log('Successfully reloaded application (/) commands.');
    } catch (error) {
        console.error('Failed to register commands:', error);
    }
    
    // Start the background scheduler check loop
    setInterval(checkScheduler, CHECK_INTERVAL_MS);
    console.log(`Scheduler check interval started (${CHECK_INTERVAL_MS / 1000 / 60} minutes).`);
});

client.on('interactionCreate', async interaction => {
    if (!interaction.isCommand()) return;

    const guildId = interaction.guildId;
    const config = botConfig[guildId];
    
    // Require Administrator for setup and test commands
    if (interaction.commandName === 'setup-leaderboard' || interaction.commandName === 'test-leaderboard') {
        if (!interaction.member.permissions.has('Administrator')) {
            return interaction.reply({ content: 'You need Administrator permissions to use this command.', ephemeral: true });
        }
    }

    switch (interaction.commandName) {
        case 'setup-leaderboard':
            const winnerChannel = interaction.options.getChannel('winner_channel');
            const winnerRole = interaction.options.getRole('winner_role');
            const topN = interaction.options.getInteger('top_n');
            const countChannel = interaction.options.getChannel('count_channel');

            // Permission check: ensure the bot can manage the role
            if (interaction.guild.members.me.roles.highest.position <= winnerRole.position) {
                return interaction.reply({ 
                    content: `❌ Bot's highest role must be positioned above the ${winnerRole.name} role in the server settings hierarchy to manage it.`, 
                    ephemeral: true 
                });
            }

            const nextRunDt = getNextSunday10AMIST();
            
            botConfig[guildId] = {
                targetChannelId: winnerChannel.id,
                winnerRoleId: winnerRole.id,
                topN: topN,
                countChannelId: countChannel.id,
                nextRunDt: nextRunDt.toISOString(), // Store as ISO string
            };

            // NOTE: In a real app, you would save botConfig here.
            // fs.writeFileSync('config.json', JSON.stringify(botConfig, null, 2));

            await interaction.reply({
                content: `✅ Automated Leaderboard Setup Complete!\n`
                       + `The leaderboard is scheduled to run every **Sunday at 10:00 AM IST**.\n`
                       + `First run: <t:${Math.floor(nextRunDt.getTime() / 1000)}:F> (<t:${Math.floor(nextRunDt.getTime() / 1000)}:R>).`,
                ephemeral: true,
            });
            break;

        case 'test-leaderboard':
            if (!config) {
                return interaction.reply({ content: '❌ Leaderboard has not been set up. Please run /setup-leaderboard first.', ephemeral: true });
            }
            
            await interaction.deferReply({ ephemeral: true });
            
            const [tCh, wR, cCh] = await Promise.all([
                interaction.guild.channels.fetch(config.targetChannelId),
                interaction.guild.roles.fetch(config.winnerRoleId),
                interaction.guild.channels.fetch(config.countChannelId)
            ]);
            
            if (!tCh || !wR || !cCh) {
                return interaction.editReply('❌ Configuration error: One or more channels/roles could not be found.');
            }

            const success = await runLeaderboardLogic(interaction.guild, config, cCh, tCh, wR);

            if (success) {
                await interaction.editReply(`✅ Test run complete. Leaderboard sent to ${tCh.toString()} and roles managed.`);
            } else {
                await interaction.editReply('❌ Test run failed. Check bot permissions and console logs.');
            }
            break;

        case 'leaderboard-timer':
            if (!config) {
                return interaction.reply({ content: '❌ The automatic leaderboard has not been set up yet. Use `/setup-leaderboard`.', ephemeral: true });
            }

            const nextRun = new Date(config.nextRunDt);
            const now = new Date();
            
            if (nextRun.getTime() <= now.getTime()) {
                await interaction.reply({ 
                    content: `🚨 The last scheduled run time has passed (<t:${Math.floor(nextRun.getTime() / 1000)}:R>). The scheduler will run it on its next check (within 30 minutes).`,
                    ephemeral: true
                });
            } else {
                await interaction.reply({ 
                    content: `⏳ The next automatic leaderboard update is scheduled for **Sunday at 10:00 AM IST**.\n`
                           + `The exact time is: <t:${Math.floor(nextRun.getTime() / 1000)}:F> (<t:${Math.floor(nextRun.getTime() / 1000)}:R>).`,
                    ephemeral: true
                });
            }
            break;
    }
});

// --- SCHEDULER LOOP ---

/**
 * The background task that checks if the scheduled run time has arrived for any guild.
 */
async function checkScheduler() {
    console.log(`[SCHEDULER] Running periodic check...`);
    const now = new Date();
    
    for (const guildId in botConfig) {
        const config = botConfig[guildId];
        const nextRun = new Date(config.nextRunDt);
        
        if (now.getTime() >= nextRun.getTime()) {
            console.log(`[SCHEDULER] Triggering run for Guild ${guildId}. Time is due.`);
            
            const guild = client.guilds.cache.get(guildId);
            if (!guild) {
                console.error(`[SCHEDULER] Guild ${guildId} not found in cache.`);
                continue;
            }

            // Fetch necessary objects
            const [targetChannel, winnerRole, countChannel] = await Promise.all([
                guild.channels.fetch(config.targetChannelId),
                guild.roles.fetch(config.winnerRoleId),
                guild.channels.fetch(config.countChannelId)
            ]).catch(e => {
                console.error(`[SCHEDULER] Failed to fetch channel/role objects for ${guildId}:`, e);
                return [null, null, null];
            });

            if (targetChannel && winnerRole && countChannel) {
                await runLeaderboardLogic(guild, config, countChannel, targetChannel, winnerRole);
                // Note: runLeaderboardLogic updates config.nextRunDt internally
            } else {
                console.error(`[SCHEDULER] Critical configuration missing for ${guild.name}. Skipping run.`);
            }
        }
    }
}

// --- START BOT ---
client.login(DISCORD_TOKEN).catch(err => {
    console.error("Failed to login to Discord. Check your DISCORD_TOKEN and CLIENT_ID:", err);
});