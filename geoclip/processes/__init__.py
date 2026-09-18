# =================================================================
#
# Geo clip API: pygeoapi process plugins
#
# =================================================================

"""pygeoapi process plugins.

Wire them into a pygeoapi configuration by their dotted path::

    resources:
        clip:
            type: process
            processor:
                name: geoclip.processes.clip.ClipProcessor
        list-tables:
            type: process
            processor:
                name: geoclip.processes.list_tables.ListTablesProcessor
"""
