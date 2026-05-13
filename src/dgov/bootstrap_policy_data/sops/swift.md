---
name: swift
title: Swift Toolchain & Project Setup
summary: Swift and Xcode project rules for setup, formatting, and targeted verification.
applies_to: [swift, xcode, xcodebuild, swift-format, xcodegen, macos]
priority: must
---
## When
- editing Swift source, tests, Package.swift, project.yml, or Xcode project scaffolding
- choosing setup, lint, format, or test commands for Swift/macOS work

## Do
- prefer `xcrun swift-format` over a bare `swift-format` binary when available
- guard XcodeGen setup so it only runs when `project.yml` exists
- set `USER=$(whoami)` inside setup commands that call tools expecting USER
- keep generated Xcode project churn out of task diffs unless the task claims it

## Do Not
- assume login-shell PATH is available in worker sandboxes
- run `xcodegen generate` before the task has created `project.yml`
- add generated project files to a task unless they are part of the requested change

## Verify
- run the narrowest Swift or Xcode command that covers the changed files
- confirm format/lint commands use the same tool path configured in `.dgov/project.toml`
- check `git diff` for generated-file churn before calling done

## Escalate
- if the repo has no Package.swift, project.yml, or Xcode project yet and the task depends on one
- if the worker environment cannot resolve required Xcode tools
