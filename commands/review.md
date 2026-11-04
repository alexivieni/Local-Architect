---
name: review
description: Read the code and report what is wrong, worst first
flow: review
args: a file, a folder, or nothing for the whole project
---
Review {args} and report what is actually wrong with it.

Order findings by how much they matter: something that loses data or lets an attacker in comes before something that is merely untidy.
For each finding give the file and line, what goes wrong, and the smallest change that fixes it. Say plainly when you are not sure.
Do not report style preferences as problems, and do not pad the list to look thorough.
