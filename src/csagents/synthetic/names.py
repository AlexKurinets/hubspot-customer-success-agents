"""Invented names. Clinic names and towns are built from word lists; people are common first and last
names in random pairs; emails use example.com and phones the fictional 555-01xx range."""

CLINIC_FIRST = [
    "Thistlewick", "Copperbend", "Wrenhollow", "Larchmere", "Fernbrook", "Quillstone", "Harrowmere", "Bramblewood",
    "Saltmarrow", "Kestrelby", "Oakhaven", "Mossgate", "Riverkeel", "Juniper Hill", "Sablecrest", "Tidewell",
    "Amberleigh", "Corvid Lane", "Foxglen", "Hollin Ridge", "Pinemarch", "Starling Bay", "Willowmere", "Ashcombe",
    "Briarfield", "Cloverstead", "Dunmarsh", "Elderbrook", "Glenhallow", "Heronsgate", "Ivyreach", "Lanternfield",
]
CLINIC_SECOND = ["Veterinary Group", "Animal Hospital", "Pet Clinic", "Veterinary Partners", "Animal Care",
                 "Veterinary Centre", "Companion Animal Clinic", "Vet Practice"]
TOWNS = ["Thornbury Vale", "Quellmoor", "Ashgrove Bend", "Marrowdale", "Kettleby Cross", "Sundermere", "Fallowmere",
         "Greywater Falls", "Pellingham", "Rookwood Crossing", "Brindlecombe", "Ostwick", "Halverstone", "Wexmoor"]
FIRST = ["Maya", "Daniel", "Priya", "Owen", "Lucia", "Marcus", "Hannah", "Theo", "Imani", "Samuel", "Grace", "Nikhil",
         "Elena", "Jonah", "Rosa", "Aaron", "Leah", "Victor", "Nadia", "Caleb", "Iris", "Mateo", "Clara", "Felix"]
LAST = ["Okafor", "Lindqvist", "Ramos", "Whitlock", "Haddad", "Brennan", "Castillo", "Moreau", "Ibarra", "Kowalski",
        "Nakamura", "Ferreira", "Dunmore", "Ashworth", "Vance", "Petrov", "Quinlan", "Albrecht", "Mbeki", "Sorensen"]

CSMS = [("Maya", "Okafor"), ("Daniel", "Lindqvist"), ("Priya", "Ramos"), ("Owen", "Whitlock"),
        ("Lucia", "Haddad"), ("Marcus", "Brennan"), ("Hannah", "Castillo"), ("Theo", "Moreau")]
ACCOUNT_MANAGERS = [("Imani", "Ibarra"), ("Samuel", "Kowalski")]

TICKET_NEUTRAL = [
    ("Appointment reminders", "Reminder texts went out an hour late this morning. Can you check the schedule settings?"),
    ("Report export", "How do I export the monthly vaccination report to a spreadsheet?"),
    ("New user setup", "We hired a new technician and need a login set up with the right permissions."),
    ("Lab results", "Lab results from the reference lab are not attaching to the patient record automatically."),
    ("Inventory counts", "Inventory counts for the flea and tick products look off after the last stock take."),
    ("Printer setup", "The receipt printer at the front desk stopped working."),
]
TICKET_CUES = {
    "product_issue": [("Schedule not loading", "The schedule is not loading for half the team this morning and it keeps "
                       "crashing when we open a patient file."),
                      ("Sync failed", "Records sync failed overnight again and the team had to re-enter notes.")],
    "pricing": [("Seat costs", "The price increase on the renewal quote is a problem for us; the cost per seat is now "
                 "more than we budgeted.")],
    "competitor": [("Data export request", "We are evaluating another system and need a full export of our client and "
                    "patient data.")],
    "champion_left": [("Admin handover", "Our practice manager is leaving the practice at the end of the month, so "
                       "please move admin rights to the new practice manager.")],
}
EMAIL_CUES = {
    "product_issue": "The system has been very slow all week and two of the vets have gone back to paper notes.",
    "pricing": "Honestly the price increase this year is hard to justify; we are looking at a cheaper option.",
    "competitor": "We had a demo from another system last week and the team liked the scheduling view.",
    "champion_left": "Quick note that Dr. {name} is retiring next month and I will be your contact going forward.",
}
EMAIL_NEUTRAL = ["Could you send the guide for setting up online booking?",
                 "Thanks for the help last week, the reminders are working now.",
                 "Is there a way to add a second location to our account?",
                 "Can we get a refresher session on the reporting screens for new staff?"]
EMAIL_BILLING = "Our card was declined on the last invoice and I think we were charged twice. Can someone check?"
EMAIL_CANCEL = "We have decided to cancel our subscription at renewal and would like to know the steps to end our contract."
