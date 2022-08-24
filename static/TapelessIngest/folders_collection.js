! function(FolderTable) {
    FolderTable.FolderCollection = Backbone.Collection.extend({
        model: cntmo.prtl.FolderTable.Folder,
        url: "/tapelessingest/folders/",
        parse: function(data) {
            return data.results;
        },
        initialize: function() {},
    })
}(cntmo.prtl.FolderTable = cntmo.prtl.FolderTable || {}, jQuery);
