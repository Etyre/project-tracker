Project Tracker

I want to make a todo tracker / GTD system app / task scheduler.

* The project hierarchy:  
  * A list of major projects or projects categories, which can include sub projects.  
  * Projects and project categories can be reordered  
  * Within projects are tasks or outcomes.   
    * You can check off tasks. This archives them so they’re not visible by default, but all completed tasks and projects are saved. There’s a view to see the complete tasks of a project, including the date they were completed.  
    * Each task is tagged  with…  
      * Visible to the user, by default:  
        * A deadline, if there is one  
        * Whether this task is blocking for someone else (e.g. someone else is not able to start on some sequential work, because they’re waiting on me)  
        * An importantness ranking  
      * invisible to the user, by default, way)   
        * The estimated time to complete the task  
        * If the task needs to be done in one whole chunk or if it can be broken into pieces  
        * The date that task was created  
    * Each task can have some expandable description including relevant links and context   
    * This should integrate with linear, so that linear tasks are mirrored in this system, and updating them in this system updates them in linear.

* The input process:  
  * There should be an easy way to add new tasks, which an LLM inserts intelligently into the hierarchy.

* The feed  
  * An automated system builds a schedule for the work day, between particular hours. As inputs it takes  
    * My calendar, which has scheduled meetings  
    * My daily checklist: a google sheet of task items that I want to do either every day, or regularly, if not every day.  
    * My exercise spreadsheet, which tracks which kind of exercise I’ve done, recently and which should be scheduled next.  
    * The tasks in the project hierarchy.  
  * The scheduling should be intelligent so that 1\) the most important things and the things that are blocking for other people are prioritized, 2\) everything can fit in the space available.  
  * There’s also a user editable “rules and heuristics” doc that gives a bunch of rules about scheduling  
  * The interface for this feed shows one “card” at a time.   
    * The card shows the next task to do.   
      * There’s a button to start the task. Starting the task starts a toggl timer with the name of the task.  
        * All toggl timers started by this app have their own label.  
      * There's a checkbox for completing the task.  
      * At the bottom of the card there’s alternative options:  
        * An exercise option (whichever kind of exercise is selected for the day)  
        * Journal, which starts a journaling toggl timer, and switches to roam / logseq on desktop1.  
      * The app should track how long it takes to complete tasks to get better time estimates.