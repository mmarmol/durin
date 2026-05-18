# Bug Report: Notifications fail after email change

## Problem
Users report not receiving email notifications after they update their
email address in their profile. The notifications resume working after
approximately 1 hour.

## Steps to reproduce
1. User signs up with email A
2. System sends notifications to email A — works
3. User changes their email to B via profile settings
4. System tries to send notification — fails with "Invalid email recipient"
   or delivers to old address A instead of new address B
5. After ~1 hour, notifications start arriving at email B

## Files in the notification system
- sender.py — main send_notification() entry point
- preferences.py — user preference lookup with caching
- templates.py — notification template rendering
- user_service.py — user profile management
